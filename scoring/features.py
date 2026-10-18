"""Graph and behavioural features, computed the same way online and offline (prd/03 sections 3.2 and 5).

One `GraphState` holds a rolling window of recent payments per account. `features()` reads the state as it is (only
payments before this one, so no label or future leakage), then the caller adds the payment with `add()`. Because the
offline trainer and exporter replay transactions in time order through the *same* state, a payment gets identical
features offline and online. That is the only way the offline numbers describe the live system (CONTRACTS section 9).

The names and order in `FEATURE_NAMES` are the contract between `scoring.score()` and the quantum export (CONTRACTS
section 13), and they are the same whether the graph features come from this pandas-window fallback or from Snap ML's
Graph Feature Preprocessor on the VM (prd/03 section 3.2).
"""
from __future__ import annotations

import collections
import hashlib
import math
from typing import Optional

# Order matters: the quantum export writes the f_ columns in this order, and the models read them in this order.
FEATURE_NAMES = [
    "f_amount_log",        # log(1 + amount)
    "f_amount_rel",        # amount against the source's recent average
    "f_hour",              # hour of day / 24 (timing)
    "f_source_out_cnt",    # payments the source sent in the window (velocity)
    "f_source_out_deg",    # distinct payees of the source (fan-out)
    "f_source_in_cnt",     # payments the source received
    "f_source_in_deg",     # distinct payers of the source (fan-in)
    "f_source_out_amt",    # log(1 + total the source sent)
    "f_target_out_cnt",    # payments the target sent (pass-through)
    "f_target_in_cnt",     # payments the target received
    "f_target_in_deg",     # distinct payers of the target (gather: many into one)
    "f_scatter",           # source fan-out over its send count (spread of destinations)
    "f_new_counterparty",  # 1 if the source never paid the target in the window
    "f_cycle",             # 1 if the target has paid the source in the window (reciprocal 2-cycle)
]
RAW_FEATURES = ["f_amount_log", "f_amount_rel", "f_hour"]   # raw-gbm-v1: no graph features, the plain baseline
N_FEATURES = len(FEATURE_NAMES)

WINDOW_S = 30 * 86400.0     # how far back the rolling graph looks
MAXLEN = 256                # cap per account, so memory stays bounded on 5M-row files


class _Side:
    """One account's recent payments on one side (sent or received): (ts, counterparty, amount), newest last."""

    __slots__ = ("events",)

    def __init__(self):
        self.events: collections.deque = collections.deque(maxlen=MAXLEN)

    def prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        ev = self.events
        while ev and ev[0][0] < cutoff:
            ev.popleft()

    def add(self, ts: float, other: str, amount: float) -> None:
        self.events.append((ts, other, amount))

    def count(self) -> int:
        return len(self.events)

    def degree(self) -> int:
        return len({e[1] for e in self.events})

    def total(self) -> float:
        return math.fsum(e[2] for e in self.events)

    def has(self, other: str) -> bool:
        return any(e[1] == other for e in self.events)


class GraphState:
    """The rolling payment graph. Not thread-safe on its own; `scoring` guards it with a lock."""

    def __init__(self):
        self.out: dict = {}
        self.inc: dict = {}

    def _side(self, table: dict, acct: str) -> _Side:
        s = table.get(acct)
        if s is None:
            s = table[acct] = _Side()
        return s

    def features(self, from_acct: str, to_acct: str, amount: float, ts: float) -> dict:
        so, si = self.out.get(from_acct), self.inc.get(from_acct)
        to_out, ti = self.out.get(to_acct), self.inc.get(to_acct)
        for s in (so, si, to_out, ti):
            if s is not None:
                s.prune(ts)
        src_out_cnt = so.count() if so else 0
        src_out_deg = so.degree() if so else 0
        src_out_amt = so.total() if so else 0.0
        src_mean = (src_out_amt / src_out_cnt) if src_out_cnt else 0.0
        hour = (ts % 86400.0) / 86400.0 * 24.0
        f = {
            "f_amount_log": math.log1p(max(0.0, amount)),
            "f_amount_rel": amount / (1.0 + src_mean),
            "f_hour": hour / 24.0,
            "f_source_out_cnt": float(src_out_cnt),
            "f_source_out_deg": float(src_out_deg),
            "f_source_in_cnt": float(si.count() if si else 0),
            "f_source_in_deg": float(si.degree() if si else 0),
            "f_source_out_amt": math.log1p(src_out_amt),
            "f_target_out_cnt": float(to_out.count() if to_out else 0),
            "f_target_in_cnt": float(ti.count() if ti else 0),
            "f_target_in_deg": float(ti.degree() if ti else 0),
            "f_scatter": src_out_deg / (1.0 + src_out_cnt),
            "f_new_counterparty": 0.0 if (so and so.has(to_acct)) else 1.0,
            "f_cycle": 1.0 if (to_out and to_out.has(from_acct)) else 0.0,
        }
        return f

    def add(self, from_acct: str, to_acct: str, amount: float, ts: float) -> None:
        self._side(self.out, from_acct).add(ts, to_acct, amount)
        self._side(self.inc, to_acct).add(ts, from_acct, amount)


def vector(f: dict, names=FEATURE_NAMES) -> list:
    return [float(f[n]) for n in names]


def feature_hash(f: dict) -> str:
    """A stable hash of the feature vector, for the ledger (CONTRACTS section 8)."""
    payload = ",".join(f"{v:.6g}" for v in vector(f)).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# reason codes: the few features that are far above typical, mapped to plain words (prd/03 A-10)
_REASON_RULES = [
    ("f_cycle", lambda v, b: v >= 1.0, "cycle with a counterparty"),
    ("f_new_counterparty", lambda v, b: v >= 1.0, "new counterparty"),
    ("f_scatter", lambda v, b: v > b.get("f_scatter", 1e9), "fan-out burst"),
    ("f_target_in_deg", lambda v, b: v > b.get("f_target_in_deg", 1e9), "many sources into one account"),
    ("f_amount_rel", lambda v, b: v > b.get("f_amount_rel", 1e9), "amount far above usual"),
    ("f_source_out_cnt", lambda v, b: v > b.get("f_source_out_cnt", 1e9), "rapid sending"),
]


def reasons(f: dict, high: Optional[dict], limit: int = 3) -> list:
    """Up to `limit` short reasons. `high` holds the per-feature 'high' thresholds learned on training data."""
    high = high or {}
    out = []
    for name, test, text in _REASON_RULES:
        if test(f.get(name, 0.0), high) and text not in out:
            out.append(text)
            if len(out) >= limit:
                break
    return out
