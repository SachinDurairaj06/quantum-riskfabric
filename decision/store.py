"""Valkey reads and writes for the decision service (CONTRACTS sections 5, 6 and 8)."""
from __future__ import annotations

import json
import time
from typing import Optional

MAXLEN = 100000
HOLD_TTL_S = 24 * 3600


def _s(value) -> str:
    """Stream fields are strings; None becomes an empty string, booleans 1 or 0."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class Store:
    def __init__(self, redis):
        self.r = redis
        self.threat_level = "NORMAL"
        self.cluster_risk: dict = {}
        self.threat_ts = 0.0

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False

    async def request_state(self, acct: str) -> dict:
        """One round trip: the drill switch, the stand-in limit and the account tier."""
        pipe = self.r.pipeline(transaction=False)
        pipe.get("cfg:force_timeout")
        pipe.get("cfg:standin_low_limit")
        pipe.get(f"tier:{acct}")
        force, limit, tier = await pipe.execute()
        return {
            "force_timeout": force == "1",
            "low_limit": float(limit) if limit not in (None, "") else 10000.0,
            "tier": tier,
        }

    async def refresh_threat(self) -> None:
        entries = await self.r.xrevrange("threat", count=1)
        if not entries:
            return
        _, fields = entries[0]
        self.threat_level = fields.get("level", "NORMAL") or "NORMAL"
        try:
            clusters = json.loads(fields.get("clusters") or "[]")
        except ValueError:
            clusters = []
        self.cluster_risk = {c.get("id"): float(c.get("risk", 0) or 0) for c in clusters if c.get("id")}
        self.threat_ts = float(fields.get("ts") or time.time())

    def risk_for(self, cluster: Optional[str]) -> float:
        if not cluster:
            return 0.0
        return self.cluster_risk.get(cluster, 0.0)

    async def record_decision(self, decision_fields: dict, ledger_rec: dict, hold: Optional[dict]) -> None:
        """decisions stream, ledger.in and (for HOLD) the hold keys, in one round trip."""
        pipe = self.r.pipeline(transaction=False)
        pipe.xadd("decisions", {k: _s(v) for k, v in decision_fields.items()}, maxlen=MAXLEN, approximate=True)
        pipe.xadd("ledger.in", {"rec": json.dumps(ledger_rec, separators=(",", ":"))}, maxlen=MAXLEN, approximate=True)
        if hold is not None:
            txn_id = hold["txn_id"]
            pipe.zadd("hold:queue", {txn_id: float(hold["queue_score"])})
            pipe.hset(f"hold:{txn_id}", mapping={k: _s(v) for k, v in hold["fields"].items()})
            pipe.expire(f"hold:{txn_id}", HOLD_TTL_S)
        await pipe.execute()

    async def ledger(self, rec: dict) -> None:
        await self.r.xadd("ledger.in", {"rec": json.dumps(rec, separators=(",", ":"))}, maxlen=MAXLEN, approximate=True)

    async def payment(self, payment: dict, replica: str) -> None:
        fields = {k: _s(payment[k]) for k in ("ts", "txn_id", "from_acct", "to_acct", "amount", "currency", "format")}
        fields["replica"] = replica  # additive field: lets a standby skip its own entries
        await self.r.xadd("payments", fields, maxlen=MAXLEN, approximate=True)

    async def hold_features(self, txn_id: str, score: Optional[float], features: Optional[dict]) -> None:
        key = f"hold:{txn_id}"
        if not await self.r.exists(key):
            return
        mapping = {}
        if score is not None:
            mapping["score"] = _s(score)
        if features:
            mapping["features"] = json.dumps(features, separators=(",", ":"))
        if mapping:
            await self.r.hset(key, mapping=mapping)
