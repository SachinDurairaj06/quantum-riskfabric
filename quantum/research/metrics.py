"""Metrics and the paired account bootstrap (prd/05 sections 3.1 and 3.3)."""
from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score


class Capacity:
    """Per test day: how many held payments analysts review (1% of the day's transactions), and the day's positives."""

    def __init__(self, day: np.ndarray, capacity_by_day: dict, total_positives: int):
        self.days, self.day_idx = np.unique(day, return_inverse=True)
        missing = [d for d in self.days if d not in capacity_by_day]
        if missing:
            raise ValueError(f"no capacity for days {missing[:5]}")
        self.cap = np.array([capacity_by_day[d] for d in self.days], dtype=int)
        self.total_pos = int(total_positives)
        if self.total_pos <= 0:
            raise ValueError("total positives must be positive")


def _selected(rows: np.ndarray, day_idx: np.ndarray, score: np.ndarray, cap: np.ndarray) -> np.ndarray:
    """Indices (into rows) of the top `cap[day]` rows per day by score; ties broken by row order."""
    d = day_idx[rows]
    order = np.lexsort((np.arange(len(rows)), -score[rows], d))
    d_sorted = d[order]
    starts = np.searchsorted(d_sorted, d_sorted, side="left")
    rank = np.arange(len(rows)) - starts
    return order[rank < cap[d_sorted]]


def recall_at_capacity(label: np.ndarray, score: np.ndarray, capacity: Capacity, rows=None) -> float:
    rows = np.arange(len(label)) if rows is None else rows
    sel = _selected(rows, capacity.day_idx, score, capacity.cap)
    return float(label[rows][sel].sum()) / capacity.total_pos


def precision_at_capacity(label: np.ndarray, score: np.ndarray, capacity: Capacity) -> float:
    rows = np.arange(len(label))
    sel = _selected(rows, capacity.day_idx, score, capacity.cap)
    return float(label[sel].mean()) if len(sel) else 0.0


def pr_auc(label: np.ndarray, score: np.ndarray) -> float:
    return float(average_precision_score(label, score)) if label.sum() > 0 else float("nan")


def random_order_recall(label: np.ndarray, capacity: Capacity) -> float:
    """Expected recall at capacity if candidates were ordered at random (for the N1 sanity check)."""
    total = 0.0
    for d in range(len(capacity.days)):
        mask = capacity.day_idx == d
        n = int(mask.sum())
        if n:
            total += min(1.0, capacity.cap[d] / n) * float(label[mask].sum())
    return total / capacity.total_pos


def paired_bootstrap(label, score_a, score_b, groups, capacity: Capacity, n_boot: int = 2000, seed: int = 0) -> dict:
    """Resample accounts with replacement; return the difference a - b in recall at capacity and its 95% interval.
    The denominator (all test positives) is held fixed; both models share every resample."""
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(groups, return_inverse=True)
    members = [np.flatnonzero(inv == g) for g in range(len(uniq))]
    diffs = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(uniq), len(uniq))
        rows = np.concatenate([members[g] for g in pick])
        diffs[b] = (recall_at_capacity(label, score_a, capacity, rows) - recall_at_capacity(label, score_b, capacity, rows))
    point = recall_at_capacity(label, score_a, capacity) - recall_at_capacity(label, score_b, capacity)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": float(point), "ci95": [float(lo), float(hi)], "n_boot": n_boot}
