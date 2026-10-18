"""Train the models and export the quantum candidates from one time-ordered pass over the data (prd/03, CONTRACTS 13).

`build_features` replays every transaction in time order through one `GraphState`, so each row's features use only
earlier payments and match what `score()` computes live. `train` fits `gfp-gbm-v1` (all features) and `raw-gbm-v1`
(raw features only) on the train split, tunes `policy.json` thresholds on the validation split to the hold-rate cap,
and writes a report. `export` scores the validation and test candidates and writes `qml_candidates.csv` and
`qml_capacity.csv` to exactly the columns the quantum gate reads.

Numbers from synthetic look-alike data only check that the pipeline runs; real metrics come from HI-Small on the VM.
This is labelled in the report and never written to the README.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

import numpy as np
import pandas as pd

from . import data as datamod
from .features import FEATURE_NAMES, RAW_FEATURES, GraphState
from .model import Model

GFP_MODEL = "gfp-gbm-v1"
RAW_MODEL = "raw-gbm-v1"
CANDIDATE_FRACTION = 0.02      # top 2% of each day by score (CONTRACTS 13)
CAPACITY_FRACTION = 0.01       # 1% of each day reviewed by analysts
DEFAULT_HOLD_CAP = 0.02        # cfg:hold_cap_pct / 100
VERIFY_RATE = 0.05             # the VERIFY band on validation (documented; re-tunable on real data)
HISTORY_WINDOW_MULT = 1.0      # keep the last WINDOW_S of train+val history to seed the live graph


def build_features(df: pd.DataFrame) -> np.ndarray:
    """One time-ordered pass: an (N, len(FEATURE_NAMES)) float32 matrix aligned to df's rows."""
    state = GraphState()
    out = np.empty((len(df), len(FEATURE_NAMES)), dtype=np.float32)
    accts_from = df["from_acct"].to_numpy()
    accts_to = df["to_acct"].to_numpy()
    amounts = df["amount"].to_numpy(dtype=float)
    tss = df["ts"].to_numpy(dtype=float)
    for i in range(len(df)):
        f = state.features(accts_from[i], accts_to[i], amounts[i], tss[i])
        out[i] = [f[n] for n in FEATURE_NAMES]
        state.add(accts_from[i], accts_to[i], amounts[i], tss[i])
    return out


def _cols(names: list) -> list:
    return [FEATURE_NAMES.index(n) for n in names]


def _weights(y: np.ndarray) -> np.ndarray:
    pos = max(1, int(y.sum()))
    neg = max(1, len(y) - pos)
    w = np.where(y == 1, len(y) / (2.0 * pos), len(y) / (2.0 * neg))
    return w


def _quantile(a: np.ndarray, q: float, default: float) -> float:
    if len(a) == 0 or np.allclose(a.min(), a.max()):
        return default
    return float(np.clip(np.quantile(a, q), 0.0, 1.0))


def _pkg_file(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _load(source: str, path: str, max_rows: Optional[int]) -> pd.DataFrame:
    """Read, optionally keep only the most recent max_rows (time-ordered, so the splits stay contiguous), then split."""
    df = datamod.read(source, path)
    if max_rows and len(df) > max_rows:
        df = df.tail(int(max_rows)).reset_index(drop=True)
    return datamod.add_split(df)


def train(source: str, path: str, model_dir: str, backend=None, hold_cap: float = DEFAULT_HOLD_CAP,
          policy_path: Optional[str] = None, report_path: Optional[str] = None, max_rows: Optional[int] = None) -> dict:
    policy_path = policy_path or _pkg_file("policy.json")
    report_path = report_path or _pkg_file("report.json")
    df = _load(source, path, max_rows)
    for part in ("train", "val", "test"):
        if not (df["split"] == part).any():
            raise ValueError(f"the data has no {part} rows after the time split; need a wider date range")
    X = build_features(df)
    is_train = (df["split"] == "train").to_numpy()
    is_val = (df["split"] == "val").to_numpy()
    is_test = (df["split"] == "test").to_numpy()
    y = df["label"].to_numpy()

    gfp = Model.make(GFP_MODEL, FEATURE_NAMES, backend).fit(X[is_train], y[is_train], _weights(y[is_train]))
    raw_cols = _cols(RAW_FEATURES)
    raw = Model.make(RAW_MODEL, RAW_FEATURES, backend).fit(X[is_train][:, raw_cols], y[is_train], _weights(y[is_train]))
    gfp.save(model_dir)
    raw.save(model_dir)

    val_scores = gfp.predict(X[is_val])
    hold_at = _quantile(val_scores, 1 - hold_cap, 0.8)
    verify_at = min(hold_at, _quantile(val_scores, 1 - VERIFY_RATE, 0.4))
    with open(policy_path, "w", encoding="utf-8") as fh:
        json.dump({"version": "pol-v1", "verify_at": round(verify_at, 4), "hold_at": round(hold_at, 4)}, fh, indent=1)

    # reason thresholds: the 95th percentile on train for the features the reason rules look at
    stats = {n: float(np.quantile(X[is_train][:, FEATURE_NAMES.index(n)], 0.95))
             for n in ("f_scatter", "f_target_in_deg", "f_amount_rel", "f_source_out_cnt")}
    with open(os.path.join(model_dir, "feature_stats.json"), "w", encoding="utf-8") as fh:
        json.dump({"high": stats}, fh, indent=1)
    _save_history(df, is_train | is_val, model_dir)

    report = _report(source, df, X, gfp, raw, is_test, y, hold_at)
    report["policy"] = {"verify_at": round(verify_at, 4), "hold_at": round(hold_at, 4), "hold_cap": hold_cap}
    report["models"] = {"dir": model_dir, "backend": gfp.backend.name,
                        "files": [f"{GFP_MODEL}.model.json", f"{RAW_MODEL}.model.json"]}
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"gfp_gbm": report["gfp_gbm"], "raw_gbm": report["raw_gbm"], "p99_ms": None,
                   "synthetic": report["synthetic"]}, fh, indent=1)
    return report


def _save_history(df: pd.DataFrame, mask: np.ndarray, model_dir: str) -> None:
    hist = df[mask]
    if len(hist):
        from .features import WINDOW_S

        recent = hist[hist["ts"] >= hist["ts"].max() - WINDOW_S * HISTORY_WINDOW_MULT]
        recent[["txn_id", "ts", "from_acct", "to_acct", "amount"]].to_csv(
            os.path.join(model_dir, "history.csv"), index=False)


def _minority_f1(y: np.ndarray, score: np.ndarray, threshold: float) -> float:
    from sklearn.metrics import f1_score

    if y.sum() == 0:
        return 0.0
    return float(f1_score(y, (score >= threshold).astype(int), zero_division=0))


def _recall_at_1pct(y: np.ndarray, score: np.ndarray) -> float:
    if y.sum() == 0:
        return 0.0
    k = max(1, int(math.ceil(0.01 * len(score))))
    top = np.argsort(-score)[:k]
    return float(y[top].sum()) / float(y.sum())


def _metrics(y, score, threshold) -> dict:
    from sklearn.metrics import average_precision_score

    return {"f1": round(_minority_f1(y, score, threshold), 4),
            "pr_auc": round(float(average_precision_score(y, score)) if y.sum() else 0.0, 4),
            "recall_at_1pct": round(_recall_at_1pct(y, score), 4)}


def _report(source, df, X, gfp, raw, is_test, y, hold_at) -> dict:
    yte = y[is_test]
    gfp_te = gfp.predict(X[is_test])
    raw_te = raw.predict(X[is_test][:, _cols(RAW_FEATURES)])
    return {"source": source, "synthetic": source not in ("hi_small", "cards"),
            "rows": {"total": int(len(df)), "train": int((df["split"] == "train").sum()),
                     "val": int((df["split"] == "val").sum()), "test": int(is_test.sum())},
            "laundering": {"total": int(y.sum()), "test": int(yte.sum())},
            "gfp_gbm": _metrics(yte, gfp_te, hold_at), "raw_gbm": _metrics(yte, raw_te, hold_at)}


def export(source: str, path: str, out_dir: str, model_dir: str, max_rows: Optional[int] = None) -> dict:
    df = _load(source, path, max_rows)
    X = build_features(df)
    gfp = Model.load(model_dir, GFP_MODEL)
    scores = gfp.predict(X)
    df = df.assign(score=scores)
    cand = df[df["split"].isin(["val", "test"])].copy()
    if cand.empty:
        raise ValueError("no validation or test rows to export")
    keep = []
    for day, g in cand.groupby("day"):
        k = max(1, int(math.ceil(CANDIDATE_FRACTION * len(g))))
        keep.append(g.nlargest(k, "score"))
    candidates = pd.concat(keep).sort_values("ts", kind="stable").reset_index(drop=True)
    # attach each candidate's features, found by its original row position in the full time-ordered table
    pos = {txn: i for i, txn in enumerate(df["txn_id"].to_numpy())}
    rows = np.array([pos[t] for t in candidates["txn_id"].to_numpy()])
    feat = X[rows]
    for i, n in enumerate(FEATURE_NAMES):
        candidates[n] = feat[:, i].astype(float)
    os.makedirs(out_dir, exist_ok=True)
    cand_cols = ["txn_id", "ts", "day", "split", "label", "score", "from_acct"] + FEATURE_NAMES
    cand_path = os.path.join(out_dir, "qml_candidates.csv")
    candidates[cand_cols].to_csv(cand_path, index=False)

    test = df[df["split"] == "test"]
    cap_rows = []
    for day, g in test.groupby("day"):
        cap_rows.append({"day": day, "transactions": int(len(g)),
                         "capacity": max(1, int(math.ceil(CAPACITY_FRACTION * len(g)))),
                         "positives": int(g["label"].sum())})
    cap = pd.DataFrame(cap_rows).sort_values("day")
    cap_path = os.path.join(out_dir, "qml_capacity.csv")
    cap.to_csv(cap_path, index=False)
    return {"candidates": cand_path, "candidate_rows": int(len(candidates)),
            "val_candidates": int((candidates["split"] == "val").sum()),
            "test_candidates": int((candidates["split"] == "test").sum()),
            "capacity": cap_path, "test_days": int(len(cap)), "test_positives": int(cap["positives"].sum()),
            "synthetic": source not in ("hi_small", "cards")}
