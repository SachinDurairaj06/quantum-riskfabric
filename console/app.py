"""Console API and pages (prd/07 E-13 to E-15). Read-only: it never changes a decision.

Run: python -m console   (127.0.0.1, port 8500 plus RF_PORT_OFFSET)
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import math
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .notes import case_note

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
QUANTUM_LABEL = "Quantum challenger (research, simulated 8 qubits); does not affect decisions"
MAX_WINDOW = 3600  # the longest view window the console offers (1 hour), and how long per-second buckets are kept


def _pct(values: list, p: float) -> Optional[float]:
    if not values:
        return None
    data = sorted(values)
    return round(data[min(len(data) - 1, int(math.ceil(p * len(data))) - 1)], 3)


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _tail_lines(path: str, max_bytes: int) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        chunk = fh.read()
    lines = chunk.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line may be cut
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _bucket_series(buckets: dict, start: int, keys: list, cols: int = 90) -> list:
    """Downsample a {second: [counts...]} store over [start, now] into at most `cols` equal time bins,
    so a one-hour window draws a readable strip instead of 3,600 bars. `keys` is a list of (name, index) pairs."""
    hi = int(time.time())
    width = max(1, math.ceil(max(1, hi - start) / cols))
    out: dict = {}
    for sec, b in buckets.items():
        if sec < start:
            continue
        col = (sec - start) // width
        row = out.setdefault(col, {"ts": start + col * width})
        for name, i in keys:
            row[name] = row.get(name, 0) + b[i]
    return [out[c] for c in sorted(out)]


class Aggregator:
    """Reads edge.log, decisions and bank.log once each, incrementally, and keeps what the panels need in memory.
    Polling the streams for a window of history every second would be slow and wrong at 1,000 requests/s.
    Counts are kept in per-second buckets so any view window up to MAX_WINDOW is a cheap in-memory sum."""

    _ACTION_IDX = {"ALLOW": 0, "VERIFY": 1, "HOLD": 2}

    def __init__(self, redis, backfill: int = 5000):
        self.r, self.backfill = redis, backfill
        self.edge: dict = {}                                  # second -> [passed, throttled, blocked]
        self.dec: dict = {}                                   # second -> [allow, verify, hold, stand_in, miss, total]
        self.dec_paths: dict = {}                             # second -> {path: count}
        self.dec_lat = collections.deque(maxlen=200000)       # (ts, latency_ms), for percentiles over the window
        self.engine_lat = collections.deque(maxlen=5000)
        self.bank_flags = collections.deque(maxlen=5000)      # (timeout, entry, posted)
        self.ids: dict = {}
        self.handlers = {"edge.log": self._edge, "decisions": self._decision, "bank.log": self._bank}

    def _edge(self, f) -> None:
        ts = _f(f.get("ts"))
        if ts is None:
            return
        b = self.edge.setdefault(int(ts), [0, 0, 0])
        status = f.get("status")
        b[1 if status == "429" else 2 if status == "403" else 0] += 1

    def _decision(self, f) -> None:
        ts = _f(f.get("ts"), 0.0)
        sec = int(ts)
        b = self.dec.setdefault(sec, [0, 0, 0, 0, 0, 0])
        idx = self._ACTION_IDX.get(f.get("action"))
        if idx is not None:
            b[idx] += 1
        b[3] += f.get("stand_in") == "1"
        b[4] += f.get("deadline_met") == "0"
        b[5] += 1
        path = f.get("path", "?")
        p = self.dec_paths.setdefault(sec, {})
        p[path] = p.get(path, 0) + 1
        lat = _f(f.get("latency_ms"))
        if lat is not None:
            self.dec_lat.append((ts, lat))

    def _bank(self, f) -> None:
        if f.get("entry") == "engine":
            lat = _f(f.get("latency_ms"))
            if lat is not None:
                self.engine_lat.append(lat)
        self.bank_flags.append((f.get("timeout") == "1", f.get("entry"), f.get("posted") == "1"))

    async def start(self) -> None:
        for stream, fn in self.handlers.items():
            entries = await self.r.xrevrange(stream, count=self.backfill)
            self.ids[stream] = entries[0][0] if entries else "0-0"
            for _, f in reversed(entries):
                fn(f)

    async def poll(self, block_ms: int = 500) -> None:
        resp = await self.r.xread(self.ids, count=10000, block=block_ms)
        for stream, entries in resp or []:
            fn = self.handlers[stream]
            for entry_id, f in entries:
                self.ids[stream] = entry_id
                fn(f)
        cutoff = int(time.time()) - MAX_WINDOW
        for store in (self.edge, self.dec, self.dec_paths):
            for sec in [k for k in store if k < cutoff]:
                del store[sec]

    async def run(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)


def create_app(redis=None, data_dir: Optional[str] = None, engine_url: Optional[str] = None,
               repo_dir: str = REPO, bank_transport=None, perimeter_url: Optional[str] = None,
               perimeter_transport=None) -> FastAPI:
    offset = int(os.environ.get("RF_PORT_OFFSET", "0"))
    data_dir = data_dir or os.environ.get("RF_DATA", "/srv/riskfabric/data")
    engine_url = engine_url or os.environ.get("RF_ENGINE_URL", f"http://127.0.0.1:{9000 + offset}")
    perimeter_url = perimeter_url or f"http://127.0.0.1:{8000 + offset}"
    if redis is None:
        import redis.asyncio as aioredis

        redis = aioredis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    ledger_path = os.path.join(data_dir, "ledger", "ledger.jsonl")
    state = {"verify": None}
    holder: dict = {}

    agg = Aggregator(redis)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        holder["http"] = httpx.AsyncClient(base_url=engine_url, transport=bank_transport, timeout=0.5)
        holder["perimeter"] = httpx.AsyncClient(base_url=perimeter_url, transport=perimeter_transport, timeout=0.5)
        await agg.start()
        task = asyncio.create_task(agg.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await holder["http"].aclose()
            await holder["perimeter"].aclose()

    app = FastAPI(title="RiskFabric console", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/scoreboard")
    async def scoreboard():
        return FileResponse(os.path.join(STATIC, "scoreboard.html"))

    @app.get("/api/threat")
    async def threat(n: int = 120):
        entries = await redis.xrevrange("threat", count=max(1, min(n, MAX_WINDOW)))
        out = []
        for _, f in reversed(entries):
            try:
                clusters = json.loads(f.get("clusters") or "[]")
            except ValueError:
                clusters = []
            out.append({"ts": _f(f.get("ts"), 0.0), "level": f.get("level", "NORMAL"),
                        "clusters": [{k: c.get(k) for k in ("id", "key_type", "action", "share", "risk", "rps")}
                                     for c in clusters][:5],
                        "n_clusters": len(clusters)})
        return {"entries": out}

    @app.get("/api/edge")
    async def edge(seconds: int = 60):
        seconds = max(1, min(seconds, MAX_WINDOW))
        start = int(time.time()) - seconds
        totals = {"passed": 0, "throttled": 0, "blocked": 0}
        for sec, b in agg.edge.items():
            if sec >= start:
                totals["passed"] += b[0]
                totals["throttled"] += b[1]
                totals["blocked"] += b[2]
        series = _bucket_series(agg.edge, start, [("passed", 0), ("throttled", 1), ("blocked", 2)])
        return {"series": series, "totals": totals, "seconds": seconds}

    @app.get("/api/decisions")
    async def decisions(seconds: int = 60):
        seconds = max(1, min(seconds, MAX_WINDOW))
        now = time.time()
        start = int(now) - seconds
        counts = {"ALLOW": 0, "VERIFY": 0, "HOLD": 0}
        stand_in = misses = total = 0
        for sec, b in agg.dec.items():
            if sec < start:
                continue
            counts["ALLOW"] += b[0]
            counts["VERIFY"] += b[1]
            counts["HOLD"] += b[2]
            stand_in += b[3]
            misses += b[4]
            total += b[5]
        paths: dict = {}
        for sec, p in agg.dec_paths.items():
            if sec >= start:
                for k, v in p.items():
                    paths[k] = paths.get(k, 0) + v
        latencies = [lat for ts, lat in agg.dec_lat if ts >= start]
        # the hold cap is defined over 60 s (CONTRACTS section 7), independent of the chosen view window
        hstart = int(now) - 60
        recent_holds = sum(b[2] for sec, b in agg.dec.items() if sec >= hstart)
        recent = sum(b[5] for sec, b in agg.dec.items() if sec >= hstart)
        cap = _f(await redis.get("cfg:hold_cap_pct"), 2.0)
        hold_rate = (100.0 * recent_holds / recent) if recent else 0.0
        series = _bucket_series(agg.dec, start, [("ALLOW", 0), ("VERIFY", 1), ("HOLD", 2)])
        return {"counts": counts, "paths": paths, "stand_in": stand_in, "deadline_miss": misses,
                "total": total, "seconds": seconds, "series": series,
                "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95), "p99": _pct(latencies, 0.99)},
                "hold_rate_60s_pct": round(hold_rate, 3), "hold_cap_pct": cap, "hold_alert": hold_rate > cap}

    @app.get("/api/holds")
    async def holds(n: int = 50):
        members = await redis.zrevrange("hold:queue", 0, max(0, min(n, 200) - 1), withscores=True)
        out = []
        pipe = redis.pipeline(transaction=False)
        for txn_id, _ in members:
            pipe.hgetall(f"hold:{txn_id}")
            pipe.hgetall(f"qml:{txn_id}")
            pipe.zscore("hold:rank", txn_id)
        res = await pipe.execute() if members else []
        for i, (txn_id, queue_score) in enumerate(members):
            hold, qml, rank_score = res[3 * i], res[3 * i + 1], res[3 * i + 2]
            if not hold:
                continue
            try:
                reasons = json.loads(hold.get("reasons") or "[]")
            except ValueError:
                reasons = []
            out.append({
                "txn_id": txn_id, "queue_score": queue_score, "score": _f(hold.get("score")),
                "amount": _f(hold.get("amount")), "currency": hold.get("currency"), "from_acct": hold.get("from_acct"),
                "to_acct": hold.get("to_acct"), "tier": hold.get("tier") or None, "cluster": hold.get("cluster") or None,
                "stand_in": hold.get("stand_in") == "1", "reasons": reasons, "note": case_note(hold),
                "challenger": ({"score": _f(qml.get("score")), "status": qml.get("status"), "backend": qml.get("backend"),
                                "model": qml.get("model")} if qml else None),
                "rerank_score": rank_score,
            })
        return {"holds": out, "challenger_label": QUANTUM_LABEL}

    @app.get("/api/ledger")
    async def ledger():
        head = await redis.get("ledger:head")
        tail = _tail_lines(ledger_path, 65536)
        last = tail[-1] if tail else None
        return {"head": head, "records": last.get("seq") if last else 0, "last_ts": last.get("ts") if last else None,
                "path": ledger_path, "last_verify": state["verify"]}

    @app.post("/api/ledger/verify")
    async def ledger_verify():
        from ledger.verify import verify_file  # read-only check of the file

        result = await asyncio.get_running_loop().run_in_executor(None, verify_file, ledger_path)
        result["checked_at"] = time.time()
        state["verify"] = result
        return result

    @app.get("/api/bank")
    async def bank():
        books = None
        try:
            resp = await holder["http"].get("/bank/books")
            if resp.status_code == 200:
                books = resp.json()
        except httpx.HTTPError:
            books = None
        timeouts = sum(1 for t, _, _ in agg.bank_flags if t)
        fail_open = sum(1 for t, entry, posted in agg.bank_flags if t and entry == "channel" and posted)
        return {"books": books, "engine_p99_ms": _pct(list(agg.engine_lat), 0.99), "recent_timeouts": timeouts,
                "recent_fail_open": fail_open, "reachable": books is not None}

    @app.get("/api/perimeter")
    async def perimeter():
        """The perimeter stand-in's own counts (perimeter/README.md); unreachable when no perimeter runs."""
        try:
            resp = await holder["perimeter"].get("/perimeter/stats")
            if resp.status_code == 200:
                return {"reachable": True, **resp.json()}
        except httpx.HTTPError:
            pass
        return {"reachable": False}

    @app.get("/api/meta")
    async def meta():
        """Which system is running (`cfg:system`, set by deploy/start.sh or devstack), for the header."""
        try:
            system = await redis.get("cfg:system")
        except Exception:
            system = None
        return {"system": system}

    @app.get("/api/shield")
    async def shield():
        records = _tail_lines(ledger_path, 2_000_000)
        pol = _read_json(os.path.join(repo_dir, "scoring", "policy.json")) or {"verify_at": 0.4, "hold_at": 0.8}
        engine_action, shadow = {}, {}
        for rec in records:
            if rec.get("kind") == "decision" and rec.get("path") == "engine":
                engine_action[rec.get("txn_id")] = rec.get("action")
            elif rec.get("kind") == "shadow" and rec.get("shadow_score") is not None:
                shadow[rec.get("txn_id")] = float(rec["shadow_score"])
        pairs = [(engine_action[t], s) for t, s in shadow.items() if t in engine_action]

        def implied(s):
            return "HOLD" if s >= pol["hold_at"] else ("VERIFY" if s >= pol["verify_at"] else "ALLOW")

        agree = sum(1 for e, s in pairs if e == implied(s))
        would_hold = sum(1 for e, s in pairs if e != "HOLD" and implied(s) == "HOLD")
        would_release = sum(1 for e, s in pairs if e == "HOLD" and implied(s) != "HOLD")
        return {"pairs": len(pairs), "agreement": round(agree / len(pairs), 4) if pairs else None,
                "shadow_would_hold": would_hold, "shadow_would_release": would_release}

    @app.get("/api/results/summary")
    async def results_summary():
        data = _read_json(os.path.join(repo_dir, "eval", "results", "summary.json"))
        if data is None:
            # no scenario runs yet; the model report can still exist on its own (python -m scoring train)
            model = _read_json(os.path.join(repo_dir, "scoring", "report.json"))
            return JSONResponse({"empty": True, "hint": "No results yet: run eval/scenarios/run_all.sh",
                                 "model": dict(model, source="scoring/report.json") if model else None,
                                 "deploy_present": os.path.exists(os.path.join(repo_dir, "deploy", "start.sh"))},
                                status_code=404)
        return data

    @app.get("/api/quantum/summary")
    async def quantum_summary():
        data = _read_json(os.path.join(repo_dir, "quantum", "results", "summary.json"))
        if data is None:
            return JSONResponse({"empty": True, "hint": "No quantum results yet"}, status_code=404)
        return data

    @app.get("/api/quantum/control")
    async def quantum_control():
        """The positive control (python -m quantum.research.evaluate control), if it was run."""
        data = _read_json(os.path.join(repo_dir, "quantum", "results", "control_summary.json"))
        if data is None:
            return JSONResponse({"empty": True}, status_code=404)
        return data

    @app.get("/api/quantum/sweep")
    async def quantum_sweep():
        """Quantum gain against pattern complexity (python -m quantum.research.evaluate sweep), if it was run."""
        data = _read_json(os.path.join(repo_dir, "quantum", "results", "advantage_sweep.json"))
        if data is None:
            return JSONResponse({"empty": True}, status_code=404)
        return data

    return app
