"""Inputs for the circuit (prd/05 Part 2.2): standardise, PCA to 8, scale to [0, 1] by the 1st and 99th percentiles, clip.
Fitted on validation training rows only. Saved as JSON so the live worker on s390x can load it (no pickles)."""
from __future__ import annotations

import json

import numpy as np


class Preprocessor:
    def __init__(self, n_components: int = 8):
        self.k = n_components
        self.columns: list = []
        self.mean = self.scale = self.components = self.lo = self.hi = None

    def fit(self, X: np.ndarray, columns: list) -> "Preprocessor":
        X = np.asarray(X, dtype=float)
        if X.shape[1] < self.k:
            raise ValueError(f"need at least {self.k} features, got {X.shape[1]}")
        self.columns = list(columns)
        self.mean = X.mean(axis=0)
        std = X.std(axis=0)
        self.scale = np.where(std > 0, std, 1.0)
        Z = (X - self.mean) / self.scale
        _, _, vt = np.linalg.svd(Z, full_matrices=False)
        comps = vt[: self.k]
        # deterministic sign: largest-magnitude loading positive
        signs = np.sign(comps[np.arange(self.k), np.abs(comps).argmax(axis=1)])
        self.components = comps * signs[:, None]
        P = Z @ self.components.T
        self.lo, self.hi = np.percentile(P, 1, axis=0), np.percentile(P, 99, axis=0)
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        Z = (np.asarray(X, dtype=float) - self.mean) / self.scale
        P = Z @ self.components.T
        span = np.where(self.hi > self.lo, self.hi - self.lo, 1.0)
        return np.clip((P - self.lo) / span, 0.0, 1.0)

    def to_dict(self) -> dict:
        return {"version": "preprocess-v1", "n_components": self.k, "columns": self.columns,
                "mean": self.mean.tolist(), "scale": self.scale.tolist(), "components": self.components.tolist(),
                "lo": self.lo.tolist(), "hi": self.hi.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Preprocessor":
        p = cls(d["n_components"])
        p.columns = d["columns"]
        for k in ("mean", "scale", "components", "lo", "hi"):
            setattr(p, k, np.array(d[k], dtype=float))
        return p

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh)

    @classmethod
    def load(cls, path: str) -> "Preprocessor":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
