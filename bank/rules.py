"""The incumbent engine's rules R0 to R5 (prd/03 section 4.3), with their parameters in bank/rules.json.

| Rule | Fires when | Outcome |
|---|---|---|
| R0 unknown account | `from_acct` isn't in the registry | HOLD |
| R1 large amount | the amount is above the segment limit | VERIFY |
| R2 velocity | at least 10 payments from the account within 60 s of this payment's time, this one included | HOLD |
| R3 card testing | at least 5 payments of 5.00 or less from the account within 300 s of this payment's time | HOLD |
| R4 model | `raw-gbm-v1` scores at or above the bank's `hold_at` (or `verify_at`); off until A-04 delivers the model | HOLD or VERIFY |
| R5 enrichment | `X-Cluster-Risk` at least 0.8 gives HOLD; `X-Edge-Action: stepup` gives at least VERIFY. Only RiskFabric sends these | HOLD or VERIFY |

Windows use the payment's own `ts` (dataset time), not the wall clock. The strictest outcome wins, and the result
lists every rule that fired.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict, deque
from typing import Optional

ORDER = {"ALLOW": 0, "VERIFY": 1, "HOLD": 2}
RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")


def load_spec(path: str = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class History:
    """Each account's most recent payment times: only as many as R2 and R3 need, for at most `max_accounts`."""

    def __init__(self, keep_all: int, keep_small: int, max_accounts: int = 200_000):
        self.keep_all, self.keep_small, self.max_accounts = keep_all, keep_small, max_accounts
        self.all: OrderedDict = OrderedDict()
        self.small: OrderedDict = OrderedDict()

    def _push(self, table: OrderedDict, maxlen: int, acct: str, ts: float) -> deque:
        q = table.get(acct)
        if q is None:
            q = table[acct] = deque(maxlen=maxlen)
            if len(table) > self.max_accounts:
                table.popitem(last=False)
        else:
            table.move_to_end(acct)
        q.append(ts)
        return q

    def record(self, acct: str, ts: float, small: bool) -> tuple:
        recent = self._push(self.all, self.keep_all, acct, ts)
        smalls = self._push(self.small, self.keep_small, acct, ts) if small else self.small.get(acct)
        return recent, smalls


def _within(q: Optional[deque], ts: float, window_s: float) -> int:
    return 0 if q is None else sum(1 for t in q if abs(ts - t) <= window_s)


class Rules:
    def __init__(self, spec: dict, registry, model=None):
        self.spec, self.registry, self.model = spec, registry, model
        self.limits = spec["R1_large_amount"]
        self.r2, self.r3, self.r4, self.r5 = (spec["R2_velocity"], spec["R3_card_testing"], spec["R4_model"],
                                              spec["R5_enrichment"])
        self.history = History(self.r2["min_payments"], self.r3["min_payments"])

    def evaluate(self, p: dict, cluster_risk: float = 0.0, edge_action: Optional[str] = None) -> tuple:
        """(action, rule ids that fired, model score or None)."""
        fired = []
        acct, ts, amount = p["from_acct"], float(p["ts"]), float(p["amount"])
        segment = self.registry.segment(acct)
        if segment is None:
            fired.append(("R0", "HOLD"))
        elif amount > self.limits.get(segment, self.limits["retail"]):
            fired.append(("R1", "VERIFY"))
        recent, smalls = self.history.record(acct, ts, amount <= self.r3["max_amount"])
        if _within(recent, ts, self.r2["window_s"]) >= self.r2["min_payments"]:
            fired.append(("R2", "HOLD"))
        if _within(smalls, ts, self.r3["window_s"]) >= self.r3["min_payments"]:
            fired.append(("R3", "HOLD"))
        score = None
        if self.model is not None and self.r4.get("hold_at") is not None:
            score = float(self.model.score(p))
            if score >= self.r4["hold_at"]:
                fired.append(("R4", "HOLD"))
            elif self.r4.get("verify_at") is not None and score >= self.r4["verify_at"]:
                fired.append(("R4", "VERIFY"))
        if cluster_risk >= self.r5["hold_at_cluster_risk"]:
            fired.append(("R5", "HOLD"))
        elif edge_action == "stepup":
            fired.append(("R5", "VERIFY"))
        action = max((a for _, a in fired), key=ORDER.__getitem__, default="ALLOW")
        return action, [rule for rule, _ in fired], score
