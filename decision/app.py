"""FastAPI decision service and Shield router (prd/07 E-01 to E-09; CONTRACTS sections 3, 4, 7 and 11).

Run: python -m decision            (port from RF_REPLICA plus RF_PORT_OFFSET, bound to 127.0.0.1)
 or: uvicorn decision.app:create_app_from_env --factory --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import importlib
import json
import logging
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config as config_mod
from . import policy as pol
from .bank import BankClient, EngineUnavailable
from .store import Store

log = logging.getLogger("decision")

CONTRACT = 2
VERIFY_WINDOW_S = 60.0
LAB_TOKEN = "lab-ok"
SHADOW_TIMEOUT_MS = 1000
BACKGROUND_WORKERS = 8
BACKGROUND_QUEUE = 20000


class Payment(BaseModel):
    """The /pay body (CONTRACTS section 3). Unknown extra fields are ignored."""

    model_config = {"extra": "ignore"}

    txn_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")   # any sane id (lab t-NNN, or other sources like sim-...-N)
    ts: float
    from_acct: str = Field(min_length=1)
    to_acct: str = Field(min_length=1)
    amount: float = Field(ge=0)
    currency: str = Field(min_length=1)
    format: str = Field(min_length=1)

    @field_validator("ts", "amount")
    @classmethod
    def finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class Stats:
    def __init__(self, replica: str):
        self.replica = replica
        self.counts = {"allow": 0, "verify": 0, "hold": 0, "stand_in": 0, "errors": 0, "settle_failed": 0}
        self.latencies = collections.deque(maxlen=10000)
        self.deadline_miss = 0

    def record(self, action: str, stand_in: bool, latency_ms: float, met: bool) -> None:
        self.counts[action.lower()] += 1
        if stand_in:
            self.counts["stand_in"] += 1
        if not met:
            self.deadline_miss += 1
        self.latencies.append(latency_ms)

    def snapshot(self) -> dict:
        data = sorted(self.latencies)

        def pct(p: float) -> float:
            if not data:
                return 0.0
            return round(data[min(len(data) - 1, int(math.ceil(p * len(data))) - 1)], 3)

        return {
            "ts": time.time(),
            "replica": self.replica,
            "counts": dict(self.counts),
            "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)},
            "deadline_miss": self.deadline_miss,
        }


def _edge_ts(raw: Optional[str], received: float) -> float:
    """X-Edge-Ts if it is a sane epoch time, otherwise our own receive time."""
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return received
    if ts > received + 1.0 or ts < received - 60.0:
        return received
    return ts


def _int(raw: Optional[str], default: int) -> int:
    try:
        value = int(float(raw))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class Service:
    def __init__(self, cfg, redis, bank_transport=None, scoring_module=None):
        self.cfg = cfg
        self.store = Store(redis)
        self.bank = BankClient(cfg.engine_url, transport=bank_transport)
        self.stats = Stats(cfg.replica)
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="score")
        self.scoring = scoring_module
        self.scoring_error: Optional[str] = None
        self.model_loaded = False
        self.policy = dict(pol.DEFAULT_POLICY)
        self.pending: dict = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=BACKGROUND_QUEUE)
        self.tasks: list = []
        self.graph = "history-only"

    # ---------- lifecycle ----------
    async def start(self) -> None:
        if self.cfg.variant == "submitted_design":
            # E-10 needs A's fast-model hook (A-13), whose name the contract doesn't fix yet.
            raise RuntimeError("RF_VARIANT=submitted_design is not available until A-13 defines the fast-model hook")
        if self.scoring is None:
            try:
                self.scoring = importlib.import_module("scoring")
            except Exception as exc:  # the stack must still come up and fall back to stand-in
                self.scoring_error = f"import scoring failed: {exc}"
                log.error(self.scoring_error)
        if self.scoring is not None:
            policy_path = os.path.join(os.path.dirname(getattr(self.scoring, "__file__", "") or "."), "policy.json")
            self.policy = pol.load_policy(policy_path)
            try:
                await asyncio.get_running_loop().run_in_executor(self.executor, self.scoring.load, self.cfg.model_dir)
                self.model_loaded = True
            except Exception as exc:
                self.scoring_error = f"scoring.load failed: {exc}"
                log.error(self.scoring_error)
        if self.model_loaded:
            if self.cfg.role == "primary":
                self.graph = "live"
            elif hasattr(self.scoring, "ingest"):
                self.graph = "following"
        self.tasks.append(asyncio.create_task(self._threat_loop()))
        for _ in range(BACKGROUND_WORKERS):
            self.tasks.append(asyncio.create_task(self._background_worker()))
        if self.cfg.role == "standby" and self.graph == "following":
            self.tasks.append(asyncio.create_task(self._follow_payments()))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.bank.close()
        self.executor.shutdown(wait=False)

    # ---------- background ----------
    async def _threat_loop(self) -> None:
        while True:
            try:
                await self.store.refresh_threat()
            except Exception as exc:
                log.debug("threat refresh failed: %s", exc)
            await asyncio.sleep(0.5)

    async def _background_worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await job()
            except Exception:
                log.exception("background job failed")
            finally:
                self.queue.task_done()

    def submit(self, job, is_settle: bool = False) -> None:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.stats.counts["settle_failed" if is_settle else "errors"] += 1
            log.error("background queue full; job dropped")

    async def _follow_payments(self) -> None:
        newest = await self.store.r.xrevrange("payments", count=1)
        last_id = newest[0][0] if newest else "0-0"  # "$" would skip entries that arrive between reads
        loop = asyncio.get_running_loop()
        while True:
            try:
                resp = await self.store.r.xread({"payments": last_id}, count=500, block=1000)
            except Exception as exc:
                log.warning("payments follow failed: %s", exc)
                await asyncio.sleep(1.0)
                continue
            for _, entries in resp or []:
                for entry_id, f in entries:
                    last_id = entry_id
                    if f.get("replica") == self.cfg.replica:
                        continue
                    payment = {
                        "txn_id": f.get("txn_id"), "ts": float(f.get("ts") or 0), "from_acct": f.get("from_acct"),
                        "to_acct": f.get("to_acct"), "amount": float(f.get("amount") or 0),
                        "currency": f.get("currency"), "format": f.get("format"),
                    }
                    try:
                        await loop.run_in_executor(self.executor, self.scoring.ingest, payment)
                    except Exception as exc:
                        log.warning("ingest %s failed: %s", payment["txn_id"], exc)

    # ---------- scoring ----------
    async def score(self, payment: dict, signals: dict, timeout_ms: float) -> dict:
        if not self.model_loaded:
            raise RuntimeError("model not loaded")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.executor, self.scoring.score, payment, signals, int(timeout_ms))
        return await asyncio.wait_for(fut, timeout=timeout_ms / 1000.0)

    def is_unavailable(self, exc: BaseException) -> bool:
        unavailable = getattr(self.scoring, "Unavailable", None)
        return isinstance(exc, asyncio.TimeoutError) or (unavailable is not None and isinstance(exc, unavailable))

    async def shadow_job(self, payment: dict, signals: dict, req_id: str, held: bool) -> None:
        if not self.model_loaded:   # Shield mode runs without A's model; /health already reports why
            return
        try:
            res = await self.score(payment, signals, SHADOW_TIMEOUT_MS)
        except Exception as exc:
            if not self.is_unavailable(exc):
                log.warning("shadow score %s failed: %s", payment["txn_id"], exc)
            return
        await self.store.ledger({
            "ts": time.time(), "kind": "shadow", "txn_id": payment["txn_id"], "req_id": req_id,
            "shadow_score": float(res["score"]), "model": res.get("model"),
        })
        if self.cfg.role == "primary":
            await self.store.payment(payment, self.cfg.replica)
        if held:
            await self.store.hold_features(payment["txn_id"], float(res["score"]), res.get("features"))

    async def settle_job(self, payment: dict, action: str, path: str, req_id: str) -> None:
        ok = await self.bank.settle(payment, action, path, self.cfg.mode, req_id)
        if not ok:
            self.stats.counts["settle_failed"] += 1

    def expire_pending(self, now: float) -> None:
        for txn_id in [t for t, entry in self.pending.items() if entry["expires"] < now]:
            del self.pending[txn_id]


def create_app(cfg=None, redis=None, bank_transport=None, scoring_module=None) -> FastAPI:
    cfg = cfg or config_mod.load()
    if redis is None:
        import redis.asyncio as aioredis

        redis = aioredis.Redis.from_url(cfg.valkey_url, decode_responses=True)
    svc_box: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        svc = Service(cfg, redis, bank_transport=bank_transport, scoring_module=scoring_module)
        svc_box["svc"] = svc
        _app.state.svc = svc
        await svc.start()
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(title="RiskFabric decision service", lifespan=lifespan)

    @app.post("/pay")
    async def pay(request: Request):
        svc: Service = svc_box["svc"]
        received = time.time()
        try:
            payment = Payment.model_validate(await request.json()).model_dump()
        except (ValueError, ValidationError) as exc:
            return JSONResponse({"error": "invalid body", "detail": str(exc)[:300]}, status_code=400)
        h = request.headers
        t0 = _edge_ts(h.get("x-edge-ts"), received)
        deadline_ms = _int(h.get("x-deadline-ms"), cfg.deadline_ms)
        req_id = h.get("x-request-id") or str(uuid.uuid4())
        cluster = h.get("x-cluster") or None
        edge_action = "stepup" if (h.get("x-edge-action") or "").lower() == "stepup" else None
        signals = {"cluster": cluster, "client_rate": None, "edge_action": edge_action}
        try:
            state = await svc.store.request_state(payment["from_acct"])
        except Exception as exc:
            svc.stats.counts["errors"] += 1
            log.error("valkey unavailable: %s", exc)
            return JSONResponse({"error": "service not ready"}, status_code=503)

        score = model = feature_hash = features = engine = None
        reasons: list = []
        tier = None
        scored = False
        stand_in = state["force_timeout"]
        if not stand_in and cfg.mode == "standalone":
            elapsed_ms = max(0.0, (time.time() - t0) * 1000.0)
            timeout_ms = max(5.0, deadline_ms - elapsed_ms - 10.0)
            try:
                res = await svc.score(payment, signals, timeout_ms)
                score = float(res["score"])
                model = res.get("model")
                reasons = list(res.get("reasons") or [])[:5]
                feature_hash = res.get("feature_hash")
                features = res.get("features")
                action, path, scored = pol.action_from_score(score, svc.policy, edge_action), "full", True
            except Exception as exc:
                if not svc.is_unavailable(exc):
                    svc.stats.counts["errors"] += 1
                    log.warning("score %s failed: %s", payment["txn_id"], exc)
                stand_in = True
        elif not stand_in:  # Shield mode: the bank's engine decides
            elapsed_ms = max(0.0, (time.time() - t0) * 1000.0)
            budget_ms = deadline_ms - elapsed_ms - 10.0
            headers = {"X-Request-Id": req_id, "X-Cluster-Risk": f"{svc.store.risk_for(cluster):.3f}"}
            if cluster:
                headers["X-Cluster"] = cluster
            if edge_action:
                headers["X-Edge-Action"] = edge_action
            started = time.perf_counter()
            try:
                eng = await svc.bank.engine_decision(payment, headers, budget_ms)
                action, path = eng["decision"], "engine"
                score = float(eng["score"]) if isinstance(eng["score"], (int, float)) else None
                model, reasons = "bank-engine", eng["reasons"]
                engine = {"decision": action, "latency_ms": eng["latency_ms"], "timeout": False}
            except EngineUnavailable as exc:
                log.info("engine unavailable for %s: %s", payment["txn_id"], exc)
                engine = {"decision": None, "latency_ms": round((time.perf_counter() - started) * 1000.0, 3), "timeout": True}
                stand_in = True
        if stand_in:
            tier = pol.normalise_tier(state["tier"])
            action, path = pol.stand_in(tier, payment["amount"], state["low_limit"]), "stand_in"
            score = model = feature_hash = None
            reasons = pol.stand_in_reasons(tier, cfg.mode)

        latency_ms = round((time.time() - t0) * 1000.0, 3)
        met = latency_ms <= deadline_ms
        load = svc.store.threat_level
        now = time.time()
        response = {
            "contract": CONTRACT, "txn_id": payment["txn_id"], "action": action, "path": path,
            "stand_in": stand_in, "score": score, "model": model, "reasons": reasons, "tier": tier,
            "cluster": cluster, "load": load, "latency_ms": latency_ms, "deadline_ms": deadline_ms,
            "deadline_met": met, "mode": cfg.mode, "engine": engine,
        }
        decision_fields = {
            "ts": now, "txn_id": payment["txn_id"], "req_id": req_id, "action": action, "path": path,
            "stand_in": stand_in, "score": score, "model": model, "tier": tier, "cluster": cluster, "load": load,
            "latency_ms": latency_ms, "deadline_met": met, "mode": cfg.mode, "replica": cfg.replica,
        }
        ledger_rec = {
            "ts": now, "kind": "decision", "txn_id": payment["txn_id"], "req_id": req_id, "action": action,
            "path": path, "stand_in": stand_in, "score": score, "model": model, "policy": svc.policy["version"],
            "feature_hash": feature_hash, "reasons": reasons, "cluster": cluster, "mode": cfg.mode,
            "engine": engine, "shadow_score": None,
        }
        hold = None
        if action == "HOLD":
            own_score = score if (scored and cfg.mode == "standalone") else None
            queue_score = own_score if own_score is not None else (score if score is not None else 0.5)
            hold = {
                "txn_id": payment["txn_id"], "queue_score": queue_score,
                "fields": {
                    "ts": now, "from_acct": payment["from_acct"], "to_acct": payment["to_acct"],
                    "amount": payment["amount"], "currency": payment["currency"], "format": payment["format"],
                    "score": own_score, "reasons": json.dumps(reasons), "stand_in": stand_in,
                    "tier": tier, "cluster": cluster,
                    "features": json.dumps(features) if (features and own_score is not None) else None,
                },
            }
        try:
            await svc.store.record_decision(decision_fields, ledger_rec, hold)
        except Exception as exc:
            svc.stats.counts["errors"] += 1
            log.error("could not record %s: %s", payment["txn_id"], exc)
        svc.stats.record(action, stand_in, latency_ms, met)

        if action == "VERIFY":
            svc.expire_pending(now)
            svc.pending[payment["txn_id"]] = {"expires": now + VERIFY_WINDOW_S, "payment": payment,
                                              "req_id": req_id, "path": path}
        svc.submit(lambda: svc.settle_job(payment, action, path, req_id), is_settle=True)
        if cfg.mode == "shield":
            svc.submit(lambda: svc.shadow_job(payment, signals, req_id, action == "HOLD"))
        elif scored and cfg.role == "primary":
            svc.submit(lambda: svc.store.payment(payment, cfg.replica))
        return response

    @app.post("/verify")
    async def verify(request: Request):
        svc: Service = svc_box["svc"]
        try:
            body = await request.json()
            txn_id, token = str(body.get("txn_id", "")), str(body.get("token", ""))
        except (ValueError, AttributeError):
            return JSONResponse({"verified": False}, status_code=403)
        now = time.time()
        svc.expire_pending(now)
        entry = svc.pending.get(txn_id)
        if entry is None or token != LAB_TOKEN:
            return JSONResponse({"verified": False}, status_code=403)
        del svc.pending[txn_id]
        await svc.store.ledger({"ts": now, "kind": "verify", "txn_id": txn_id, "req_id": entry["req_id"],
                                "action": "ALLOW", "verified": True})
        payment, path, req_id = entry["payment"], entry["path"], entry["req_id"]
        svc.submit(lambda: svc.settle_job(payment, "ALLOW", path, req_id), is_settle=True)
        return {"txn_id": txn_id, "action": "ALLOW", "verified": True}

    @app.get("/health")
    async def health():
        svc: Service = svc_box["svc"]
        valkey = await svc.store.ping()
        ok = valkey and (svc.model_loaded or cfg.mode == "shield")
        body = {"ok": ok, "mode": cfg.mode, "role": cfg.role, "graph": svc.graph,
                "model_loaded": svc.model_loaded, "valkey": valkey, "version": "dev"}
        if svc.scoring_error:
            body["error"] = svc.scoring_error
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/stats")
    async def stats():
        return svc_box["svc"].stats.snapshot()

    return app


def create_app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per bank call is too much at load
    return create_app()
