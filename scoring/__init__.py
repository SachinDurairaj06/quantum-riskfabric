"""The classical risk engine E calls (CONTRACTS section 9): load(), score(), ingest(), Unavailable, policy.json.

`score()` reads the live graph for this payment's features, scores with `gfp-gbm-v1`, then adds the payment to the
graph — the same features the offline trainer and exporter compute, so the offline numbers describe the live system.
It makes no network calls and gives the same answer at any load. `ingest()` lets the standby replica follow without
scoring. The graph is seeded at `load()` from the recent history saved during training.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from typing import Optional

from . import features as feat
from .features import FEATURE_NAMES
from .model import Model
from .pipeline import GFP_MODEL, RAW_MODEL

FAST_MODEL = RAW_MODEL   # the downgrade baseline's fast model (CONTRACTS section 9); never loaded by default


class Unavailable(Exception):
    """score() could not answer: the model is not loaded, or it ran out of time."""


class _Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.graph = feat.GraphState()
        self.model: Optional[Model] = None
        self.high: dict = {}
        self.feature_cols: list = FEATURE_NAMES

    def load(self, model_dir: str, model_name: str = GFP_MODEL) -> None:
        model = Model.load(model_dir, model_name)
        stats_path = os.path.join(model_dir, "feature_stats.json")
        high = {}
        if os.path.exists(stats_path):
            with open(stats_path, encoding="utf-8") as fh:
                high = json.load(fh).get("high", {})
        graph = feat.GraphState()
        hist = os.path.join(model_dir, "history.csv")
        if os.path.exists(hist):
            with open(hist, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        graph.add(row["from_acct"], row["to_acct"], float(row["amount"]), float(row["ts"]))
                    except (KeyError, ValueError):
                        continue
        with self.lock:
            self.model, self.high, self.graph, self.feature_cols = model, high, graph, model.features

    def score(self, payment: dict, timeout_ms: int) -> dict:
        start = time.perf_counter()
        if self.model is None:
            raise Unavailable("model not loaded")
        amount, ts = float(payment["amount"]), float(payment["ts"])
        with self.lock:
            f = self.graph.features(payment["from_acct"], payment["to_acct"], amount, ts)
            x = [[f[n] for n in self.feature_cols]]
            self.graph.add(payment["from_acct"], payment["to_acct"], amount, ts)
            model = self.model
        s = float(model.predict(x)[0])
        elapsed = (time.perf_counter() - start) * 1000.0
        if elapsed > timeout_ms:
            raise Unavailable(f"scoring took {elapsed:.1f} ms, over the {timeout_ms} ms budget")
        return {"score": s, "reasons": feat.reasons(f, self.high)[:5], "model": model.name,
                "feature_hash": feat.feature_hash(f), "latency_ms": round(elapsed, 3),
                "features": {n: round(float(v), 6) for n, v in f.items()}}

    def ingest(self, payment: dict) -> None:
        with self.lock:
            self.graph.add(payment["from_acct"], payment["to_acct"], float(payment["amount"]), float(payment["ts"]))


_ENGINE = _Engine()


def load(model_dir: str, model_name: str = GFP_MODEL) -> None:
    _ENGINE.load(model_dir, model_name)


def score(payment: dict, signals: dict, timeout_ms: int = 40) -> dict:
    # signals (cluster, client_rate, edge_action) steer the bank's engine, not this score, so the answer is the same
    # at any load and offline matches online. They are accepted to honour the interface.
    return _ENGINE.score(payment, timeout_ms)


def ingest(payment: dict) -> None:
    _ENGINE.ingest(payment)
