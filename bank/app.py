"""The synthetic bank (CONTRACTS section 11): the channel, the incumbent engine and the books in one process.

Channel `POST /bank/pay` runs the engine within `bank:cfg:deadline_ms`, measured from the edge's `X-Edge-Ts`. If the
engine is late it applies `bank:cfg:fail_mode` (prd/03 section 4.4), with `engine.timeout` true:
- `open`, the status quo: ALLOW;
- `closed`, the sensitivity run: HOLD;
- `bounded`, the realistic bank (BK-11): a registry account at most `bank:cfg:fallback_limit` gets ALLOW, a registry
  account above it VERIFY, an account not in the registry HOLD.
The channel posts ALLOW itself; VERIFY waits up to 60 s for `POST /bank/verify`; HOLD is never posted during a run.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import settings as settings_mod
from .banklog import BankLog
from .books import Books
from .capacity import Capacity
from .engine import Engine
from .registry import Registry
from .rules import Rules, load_spec
from .settings import Settings

log = logging.getLogger("bank")

PAYMENT_FIELDS = {"txn_id": str, "ts": (int, float), "from_acct": str, "to_acct": str, "amount": (int, float),
                  "currency": str, "format": str}
VERIFY_TOKEN = "lab-ok"      # the simulated step-up answer (CONTRACTS section 4)
VERIFY_WINDOW_S = 60.0


def payment_error(p) -> Optional[str]:
    if not isinstance(p, dict):
        return "body must be a JSON object"
    for name, kind in PAYMENT_FIELDS.items():
        if name not in p:
            return f"missing field {name}"
        if not isinstance(p[name], kind) or isinstance(p[name], bool):
            return f"field {name} has the wrong type"
    if not math.isfinite(float(p["amount"])) or p["amount"] <= 0:
        return "amount must be a positive number"
    return None


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quiet(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class Bank:
    def __init__(self, r, data_dir: str, spec: Optional[dict] = None):
        self.r = r
        self.settings = Settings()
        self.capacity = Capacity(self.settings.slots)
        self.log = BankLog(r)
        self.registry = Registry.load(data_dir)
        self.rules = Rules(spec or load_spec(), self.registry, model=None)   # raw-gbm-v1 plugs in here (A-04)
        self.engine = Engine(self.rules, self.capacity, self.settings, self.log)
        self.books = Books(r, os.path.join(data_dir, "bank", "postings.jsonl"), self.log)
        self.pending: dict = {}   # txn_id -> (payment, req_id, expires_at, posted_by_fallback)

    def fallback(self, p: dict) -> str:
        mode = self.settings.fail_mode
        if mode == "open":
            return "ALLOW"
        if mode == "closed":
            return "HOLD"
        if self.registry.segment(p["from_acct"]) is None:
            return "HOLD"
        return "ALLOW" if p["amount"] <= self.settings.fallback_limit else "VERIFY"

    async def channel(self, p: dict, req_id: str, headers) -> dict:
        received = time.time()
        edge_ts = _float(headers.get("x-edge-ts")) or received
        budget_s = max(0.0, self.settings.deadline_ms / 1000.0 - max(0.0, received - edge_ts))
        task = asyncio.ensure_future(self.engine.evaluate(
            p, req_id, cluster_risk=_float(headers.get("x-cluster-risk")) or 0.0,
            edge_action=headers.get("x-edge-action")))
        timeout = False
        try:
            res = await asyncio.wait_for(asyncio.shield(task), timeout=budget_s)
            action, rules, score, engine_ms, queue_ms = res.action, res.rules, res.score, res.latency_ms, res.queue_ms
        except asyncio.TimeoutError:
            task.add_done_callback(_quiet)    # the engine still finishes and logs its own entry
            timeout, action, rules, score = True, self.fallback(p), [], None
            engine_ms = queue_ms = round(budget_s * 1000.0, 3)
        posted = False
        if action == "ALLOW":
            posted = await self.books.post(p, req_id, via="bank_channel", path="bank", fallback=timeout)
        elif action == "VERIFY":
            self.pending[p["txn_id"]] = (p, req_id, time.time() + VERIFY_WINDOW_S, timeout)
        latency_ms = (time.time() - edge_ts) * 1000.0
        self.log.add(txn_id=p["txn_id"], req_id=req_id, entry="channel", action=action, latency_ms=latency_ms,
                     queue_ms=queue_ms, timeout=timeout, rules=rules, posted=posted)
        return {"contract": 2, "txn_id": p["txn_id"], "action": action, "path": "bank", "stand_in": False,
                "score": score, "model": None, "reasons": (["timeout", f"fallback {self.settings.fail_mode}"]
                                                           if timeout else rules[:5]),
                "tier": None, "cluster": None, "load": None, "latency_ms": round(latency_ms, 3),
                "deadline_ms": int(self.settings.deadline_ms), "deadline_met": not timeout, "mode": "bank_only",
                "engine": {"decision": None if timeout else action, "latency_ms": engine_ms, "timeout": timeout}}

    async def verify(self, txn_id: str, token) -> tuple:
        item = self.pending.get(txn_id)
        if item is None or item[2] < time.time() or token != VERIFY_TOKEN:
            self.log.add(txn_id=txn_id, req_id=item[1] if item else "", entry="verify", action="VERIFY")
            return 403, {"verified": False}
        p, req_id, _, fallback = self.pending.pop(txn_id)
        posted = await self.books.post(p, req_id, via="bank_channel", path="bank", fallback=fallback)
        self.log.add(txn_id=txn_id, req_id=req_id, entry="verify", action="ALLOW", posted=posted)
        return 200, {"txn_id": txn_id, "action": "ALLOW", "verified": True}

    def sweep(self) -> None:
        now = time.time()
        for txn_id in [t for t, item in self.pending.items() if item[2] < now]:
            del self.pending[txn_id]

    def books_snapshot(self) -> dict:
        self.sweep()
        return {"ts": time.time(), "accounts": len(self.registry), "posted_count": self.books.posted_count,
                "posted_value": round(self.books.posted_value, 2), "pending_count": len(self.pending),
                "fail_open_count": self.books.fallback_count, "fail_open_value": round(self.books.fallback_value, 2)}


async def _sweep_forever(bank: Bank) -> None:
    while True:
        await asyncio.sleep(5.0)
        bank.sweep()


def create_app(redis=None, data_dir: Optional[str] = None, spec: Optional[dict] = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("RF_DATA", "/srv/riskfabric/data")
    if redis is None:
        import redis.asyncio as aioredis

        redis = aioredis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"),
                                        decode_responses=True)
    bank = Bank(redis, data_dir, spec)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            await settings_mod.read(redis, bank.settings)
            await bank.capacity.resize(bank.settings.slots)
        except Exception:
            log.exception("Valkey not reachable at start; using default bank settings")
        tasks = [asyncio.create_task(t) for t in (settings_mod.refresh_forever(redis, bank.settings, bank.capacity),
                                                   bank.log.run_forever(), _sweep_forever(bank))]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await bank.log.flush()
            bank.books.close()

    app = FastAPI(title="Synthetic bank", lifespan=lifespan)
    app.state.bank = bank

    async def body_of(request: Request):
        try:
            return await request.json()
        except ValueError:
            return None

    @app.post("/bank/pay")
    async def pay(request: Request):
        p = await body_of(request)
        err = payment_error(p)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return await bank.channel(p, request.headers.get("x-request-id", ""), request.headers)

    @app.post("/bank/verify")
    async def verify(request: Request):
        body = await body_of(request)
        if not isinstance(body, dict) or not isinstance(body.get("txn_id"), str):
            return JSONResponse({"verified": False}, status_code=403)
        status, out = await bank.verify(body["txn_id"], body.get("token"))
        return JSONResponse(out, status_code=status)

    @app.post("/engine/pay")
    async def engine_pay(request: Request):
        p = await body_of(request)
        err = payment_error(p)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        if request.headers.get("x-mock-fail") == "1":
            return JSONResponse({"error": "mock failure"}, status_code=503)
        res = await bank.engine.evaluate(p, request.headers.get("x-request-id", ""),
                                         cluster_risk=_float(request.headers.get("x-cluster-risk")) or 0.0,
                                         edge_action=request.headers.get("x-edge-action"),
                                         extra_ms=max(0.0, _float(request.headers.get("x-mock-delay-ms")) or 0.0))
        return {"decision": res.action, "score": res.score, "reasons": res.rules[:5],
                "engine_latency_ms": res.latency_ms, "queue_ms": res.queue_ms}

    @app.get("/engine/health")
    async def engine_health():
        return {"ok": True, "slots": bank.capacity.limit, "in_use": bank.capacity.in_use,
                "waiting": bank.capacity.waiting}

    @app.post("/bank/settle")
    async def settle(request: Request):
        body = await body_of(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        err = payment_error(body.get("payment"))
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return await bank.books.settle(body)

    @app.get("/bank/books")
    async def books():
        return bank.books_snapshot()

    @app.get("/bank/health")
    async def health():
        try:
            valkey = bool(await asyncio.wait_for(redis.ping(), timeout=0.5))
        except Exception:
            valkey = False
        return {"ok": valkey, "model_loaded": False, "accounts": len(bank.registry), "valkey": valkey,
                "fail_mode": bank.settings.fail_mode}

    return app
