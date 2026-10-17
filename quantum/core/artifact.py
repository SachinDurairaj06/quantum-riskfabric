"""Frozen challenger models as JSON (prd/05 Q-08), applied with numpy only, so the worker needs no scikit-learn.

qp1:  {"model": "QP-1", "version": "qp1-v1", "lam": .., "x_mean": [16], "x_scale": [16], "coef": [16], "intercept": ..}
      inputs are [classical score s, phi(u) (15 values)]; output is a probability.
qk1:  {"model": "QK-1", "version": "qk1-v1", "lam": .., "support_u": [[8]..], "dual_coef": [..], "intercept": ..}
      output is the SVM decision value squashed to 0..1 with a logistic for display.
"""
from __future__ import annotations

import json
import math

import numpy as np

from . import circuit


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class Challenger:
    def __init__(self, spec: dict):
        self.spec = spec
        self.model = spec["model"]
        self.version = spec["version"]
        self.lam = float(spec["lam"])
        if self.model == "QP-1":
            self.x_mean = np.array(spec["x_mean"], dtype=float)
            self.x_scale = np.array(spec["x_scale"], dtype=float)
            self.coef = np.array(spec["coef"], dtype=float)
            self.intercept = float(spec["intercept"])
        elif self.model == "QK-1":
            self.support_psi = circuit.statevectors(np.array(spec["support_u"], dtype=float), self.lam)
            self.dual = np.array(spec["dual_coef"], dtype=float)
            self.intercept = float(spec["intercept"])
        else:
            raise ValueError(f"unknown challenger model {self.model!r}")

    def score(self, u: np.ndarray, s: np.ndarray) -> np.ndarray:
        u = np.atleast_2d(np.asarray(u, dtype=float))
        if self.model == "QP-1":
            X = np.hstack([np.asarray(s, dtype=float).reshape(-1, 1), circuit.projected_features(u, self.lam)])
            return sigmoid(((X - self.x_mean) / self.x_scale) @ self.coef + self.intercept)
        k = circuit.kernel(circuit.statevectors(u, self.lam), self.support_psi)
        return sigmoid(k @ self.dual + self.intercept)

    @classmethod
    def load(cls, path: str) -> "Challenger":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))


def save(spec: dict, path: str) -> None:
    data = json.dumps(spec)
    if len(data) > 1_000_000:
        raise ValueError(f"artifact {path} is {len(data)} bytes; the limit is 1 MB")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
