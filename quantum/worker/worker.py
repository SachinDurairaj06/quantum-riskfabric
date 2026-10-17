"""The quantum challenger's shadow worker (prd/05 Part 5, check C-15).

Reads `decisions` (new entries only), keeps HOLDs, waits up to 2 s for `hold:{txn_id}` to carry `features` and
`score`, then writes `qml:{txn_id}` and a `challenger` ledger record. In `rerank` mode, and only if
quantum/GATE.md says PASS with rerank allowed, it also writes `hold:rank`. It never touches ALLOW, VERIFY or HOLD.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import numpy as np

from quantum.core.artifact import Challenger
from quantum.core.preprocess import Preprocessor

log = logging.getLogger("quantum.worker")
QDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = "numpy-statevector"
FEATURE_WAIT_S = 2.0
TTL_S = 24 * 3600
MAXLEN = 100000


class RerankRefused(Exception):
    pass


def load_challenger(models_dir: str):
    pre = Preprocessor.load(os.path.join(models_dir, "preprocess-v1.json"))
    for name in ("qp1-v1.json", "qk1-v1.json"):
        path = os.path.join(models_dir, name)
        if os.path.exists(path):
            return pre, Challenger.load(path)
    raise FileNotFoundError(f"no frozen challenger in {models_dir} (run `evaluate test` first)")


def gate_allows_rerank(gate_path: str) -> bool:
    if not os.path.exists(gate_path):
        return False
    text = open(gate_path, encoding="utf-8").read()
    return text.startswith("QML gate: PASS") and "RF_QML_MODE allowed: rerank" in text


class Worker:
    def __init__(self, redis, models_dir: str, mode: str, gate_path: str, clock=time.time):
        if mode not in ("shadow", "rerank"):
            raise ValueError(f"RF_QML_MODE={mode!r}: the worker runs only in shadow or rerank mode")
        if mode == "rerank" and not gate_allows_rerank(gate_path):
            raise RerankRefused("RF_QML_MODE=rerank refused: quantum/GATE.md does not say PASS with rerank allowed")
        self.r, self.mode, self.clock = redis, mode, clock
        self.pre, self.ch = load_challenger(models_dir)
        self.pending: dict = {}
        newest = self.r.xrevrange("decisions", count=1)
        self.last_id = newest[0][0] if newest else "0-0"  # "$" would skip entries that arrive between reads
        self.latencies: list = []

    def _write(self, txn_id: str, fields: dict, score: Optional[float]) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.hset(f"qml:{txn_id}", mapping={k: "" if v is None else str(v) for k, v in fields.items()})
        pipe.expire(f"qml:{txn_id}", TTL_S)
        if score is not None:
            rec = {"ts": fields["ts"], "kind": "challenger", "txn_id": txn_id, "challenger_score": score,
                   "model": self.ch.version, "backend": BACKEND}
            pipe.xadd("ledger.in", {"rec": json.dumps(rec, separators=(",", ":"))}, maxlen=MAXLEN, approximate=True)
            if self.mode == "rerank":
                pipe.zadd("hold:rank", {txn_id: score})
        pipe.execute()

    def score_hold(self, txn_id: str, hold: dict) -> Optional[float]:
        start = time.perf_counter()
        feats = json.loads(hold["features"])
        missing = [c for c in self.pre.columns if c not in feats]
        if missing:
            self._write(txn_id, {"score": None, "model": self.ch.version, "backend": BACKEND, "ms": None,
                                 "ts": self.clock(), "status": "error"}, None)
            log.warning("%s: features missing %s", txn_id, missing[:3])
            return None
        x = np.array([[float(feats[c]) for c in self.pre.columns]])
        score = float(self.ch.score(self.pre.transform(x), np.array([float(hold["score"])]))[0])
        ms = round(1000 * (time.perf_counter() - start), 3)
        self.latencies.append(ms)
        self._write(txn_id, {"score": round(score, 6), "model": self.ch.version, "backend": BACKEND, "ms": ms,
                             "ts": self.clock(), "status": "ok"}, round(score, 6))
        return score

    def step(self, block_ms: int = 200) -> None:
        resp = self.r.xread({"decisions": self.last_id}, count=500, block=block_ms)
        now = self.clock()
        for _, entries in resp or []:
            for entry_id, f in entries:
                self.last_id = entry_id
                if f.get("action") == "HOLD" and f.get("txn_id"):
                    self.pending.setdefault(f["txn_id"], now)
        for txn_id, first in list(self.pending.items()):
            hold = self.r.hgetall(f"hold:{txn_id}")
            if hold.get("features") and hold.get("score"):
                try:
                    self.score_hold(txn_id, hold)
                except Exception as exc:
                    log.warning("%s: scoring failed: %s", txn_id, exc)
                    self._write(txn_id, {"score": None, "model": self.ch.version, "backend": BACKEND, "ms": None,
                                         "ts": now, "status": "error"}, None)
                del self.pending[txn_id]
            elif now - first > FEATURE_WAIT_S:
                self._write(txn_id, {"score": None, "model": self.ch.version, "backend": BACKEND, "ms": None,
                                     "ts": now, "status": "no_features"}, None)
                del self.pending[txn_id]

    def run(self) -> None:
        log.info("quantum worker: mode %s, model %s, backend %s", self.mode, self.ch.version, BACKEND)
        while True:
            try:
                self.step()
            except Exception:
                log.exception("worker step failed; retrying")
                time.sleep(1.0)
