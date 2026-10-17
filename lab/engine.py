"""The async sender (prd/04 L-03 to L-11): emits each population at its rate, records one truth line per request."""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from typing import Optional

import httpx

from . import config
from .identities import Population
from .replay import accounts, laundering_from_end, rows
from .scenario import Scenario

FORMATS_BOT = ("Credit Card",)


class Lab:
    def __init__(self, scenario: Scenario, run_id: str, system: str, replay_path: str, truth_path: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None, bot_uses_app_fp: bool = False,
                 entry: str = "edge"):
        if entry not in config.ENTRY_PORTS:
            raise ValueError(f"entry must be one of {', '.join(config.ENTRY_PORTS)}")
        self.sc, self.run_id, self.system, self.entry = scenario, run_id, system, entry
        self.truth_path, self.transport = truth_path, transport
        self.pop = Population(scenario.seed)
        self.rng = random.Random(scenario.seed + 17)
        self.verify_rng = random.Random(scenario.seed + 29)
        self.replay_iter = rows(replay_path)
        self.virtual_ts: Optional[float] = None
        self.next_synthetic = config.SYNTHETIC_TXN_START
        self.counter = [0] * len(scenario.pops)
        self.sent = 0
        self.by_kind: dict = {}
        self.statuses: dict = {}
        self.start_ts = 0.0
        bot_pop = next((p for p in scenario.pops if p.kind == "bot"), None)
        need_bots = bot_pop is not None or any(p.kind == "launder" and p.options.get("identity") == "attacker" for p in scenario.pops)
        opts = bot_pop.options if bot_pop else {}
        self.bots = self.pop.bots(int(opts.get("identities", 2000)), int(opts.get("tool_fps", 1)), int(opts.get("networks", 3)),
                                  use_app_fp=bool(opts.get("use_app_fp", bot_uses_app_fp))) if need_bots else []
        self.testers = self.pop.card_testers(accounts(replay_path, 10)) if any(p.kind == "cardtest" for p in scenario.pops) else []
        launder_needed = sum(math.ceil(p.rps * p.active_seconds(scenario.duration_s)) + 5 for p in scenario.pops if p.kind == "launder")
        self.launder_rows = laundering_from_end(replay_path, launder_needed) if launder_needed else []

    # ---------- what to send ----------
    def _synthetic_id(self) -> str:
        txn = f"t-{self.next_synthetic:09d}"
        self.next_synthetic += 1
        return txn

    def _vts(self) -> float:
        return self.virtual_ts if self.virtual_ts is not None else time.time()

    def next_request(self, i: int, p) -> Optional[dict]:
        n = self.counter[i]
        self.counter[i] += 1
        if p.kind == "replay":
            try:
                row = next(self.replay_iter)
            except StopIteration:
                return None
            self.virtual_ts = row["ts"]
            ident = self.pop.customer(row["from_acct"])
            kind = "launder" if row["label"] == 1 else "legit"
            return {"kind": kind, "label": row["label"], "ident": ident, "body": _body(row)}
        if p.kind == "bot":
            ident = self.bots[n % len(self.bots)]
            body = {"txn_id": self._synthetic_id(), "ts": self._vts(), "from_acct": ident.acct,
                    "to_acct": self.bots[(n * 7 + 3) % len(self.bots)].acct, "amount": round(self.rng.uniform(10, 500), 2),
                    "currency": "USD", "format": FORMATS_BOT[0]}
            return {"kind": "bot", "label": 0, "ident": ident, "body": body}
        if p.kind == "cardtest":
            ident = self.testers[n % len(self.testers)]
            body = {"txn_id": self._synthetic_id(), "ts": self._vts(), "from_acct": ident.acct, "to_acct": "lab-merchant-01",
                    "amount": round(self.rng.uniform(0.5, 5.0), 2), "currency": "USD", "format": "Credit Card"}
            return {"kind": "cardtest", "label": 1, "ident": ident, "body": body}
        if p.kind == "launder":
            if not self.launder_rows:
                return None
            row = self.launder_rows.pop(0)
            if p.options.get("identity") == "attacker":
                ident = self.bots[n % len(self.bots)]
            else:
                ident = self.pop.customer(row["from_acct"])
            body = dict(_body(row), ts=self._vts())
            return {"kind": "launder", "label": 1, "ident": ident, "body": body}
        raise ValueError(p.kind)

    # ---------- sending ----------
    async def _send(self, client: httpx.AsyncClient, req: dict, sem: asyncio.Semaphore, out) -> None:
        ident, body = req["ident"], req["body"]
        req_id = str(uuid.uuid4())
        headers = {"X-Request-Id": req_id, "X-Forwarded-For": ident.ip, "X-Client-FP": ident.fp, "X-Client-ASN": str(ident.asn)}
        if ident.known:
            headers["X-Client-Known"] = "1"
        ts_sent = time.time()
        start = time.perf_counter()
        status, action, verified = 0, None, None
        try:
            resp = await client.post("/pay", json=body, headers=headers)
            status = resp.status_code
            if status == 200:
                try:
                    action = resp.json().get("action")
                except ValueError:
                    action = None
        except httpx.HTTPError:
            status = 0
        rtt_ms = round((time.perf_counter() - start) * 1000.0, 3)
        try:
            if action == "VERIFY":
                verified = False
                if req["kind"] in ("legit", "launder") and self.verify_rng.random() < config.VERIFY_PROBABILITY:
                    await asyncio.sleep(self.verify_rng.uniform(*config.VERIFY_DELAY_S))
                    try:
                        v = await client.post("/verify", json={"txn_id": body["txn_id"], "token": config.LAB_TOKEN}, headers=headers)
                        verified = v.status_code == 200
                    except httpx.HTTPError:
                        verified = False
            line = {"run_id": self.run_id, "system": self.system, "req_id": req_id, "txn_id": body["txn_id"],
                    "ts_sent": ts_sent, "kind": req["kind"], "label": req["label"], "amount": body["amount"],
                    "fp": ident.fp, "ip": ident.ip, "asn": ident.asn, "status": status, "action": action,
                    "rtt_ms": rtt_ms, "verified": verified}
            out.write(json.dumps(line, separators=(",", ":")) + "\n")
            self.statuses[str(status)] = self.statuses.get(str(status), 0) + 1
        finally:
            sem.release()

    async def run(self) -> dict:
        sc = self.sc
        if sc.peak_rps() > config.MAX_TOTAL_RPS:
            raise ValueError(f"peak {sc.peak_rps():.0f} requests/s exceeds the lab cap of {config.MAX_TOTAL_RPS}; "
                             "lower RF_LAB_RATE or the base rate, or ask D to raise MAX_TOTAL_RPS")
        os.makedirs(os.path.dirname(self.truth_path) or ".", exist_ok=True)
        sem = asyncio.Semaphore(config.MAX_IN_FLIGHT)
        limits = httpx.Limits(max_connections=config.MAX_IN_FLIGHT, max_keepalive_connections=256)
        loop = asyncio.get_running_loop()
        cpu0 = time.process_time()
        exhausted = [False] * len(sc.pops)
        credit = [0.0] * len(sc.pops)
        tasks: set = set()
        with open(self.truth_path, "a", encoding="utf-8") as out:
            async with httpx.AsyncClient(base_url=config.base_url(self.entry), transport=self.transport,
                                         timeout=config.CLIENT_TIMEOUT_S, limits=limits) as client:
                start = last = loop.time()
                self.start_ts = time.time()
                while True:
                    now = loop.time()
                    t = now - start
                    if t >= sc.duration_s:
                        break
                    dt, last = now - last, now
                    for i, p in enumerate(sc.pops):
                        if exhausted[i] or not p.active(t):
                            continue
                        credit[i] += p.rps * dt
                        while credit[i] >= 1.0:
                            credit[i] -= 1.0
                            req = self.next_request(i, p)
                            if req is None:
                                exhausted[i] = True
                                break
                            await sem.acquire()
                            task = asyncio.create_task(self._send(client, req, sem, out))
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)
                            self.sent += 1
                            self.by_kind[req["kind"]] = self.by_kind.get(req["kind"], 0) + 1
                    await asyncio.sleep(0.005)
                if tasks:
                    await asyncio.gather(*list(tasks), return_exceptions=True)
        offered = sc.offered()
        ratio = (self.sent / offered) if offered else 1.0
        return {"run_id": self.run_id, "scenario": sc.id, "system": self.system, "seed": sc.seed,
                "start_ts": self.start_ts, "duration_s": sc.duration_s, "warmup_s": sc.warmup_s,
                "offered": round(offered, 1), "sent": self.sent, "achieved_ratio": round(ratio, 4),
                "valid": ratio >= 0.95, "by_kind": self.by_kind, "statuses": self.statuses, "truth": self.truth_path,
                "lab_cpu_s": round(time.process_time() - cpu0, 3), "faults": sc.faults}


def _body(row: dict) -> dict:
    return {k: row[k] for k in ("txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format")}
