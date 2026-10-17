"""python -m eval.scenarios.summary: rebuild eval/results/summary.json and eval/RESULTS.md from the result files
(prd/02 M-R2, CONTRACTS section 12). The scoreboard renders summary.json as it is."""
from __future__ import annotations

import glob
import json
import os
import statistics
import subprocess
import sys
import time

from .metrics import flatten

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.dirname(HERE)
REPO = os.path.dirname(EVAL)
RESULTS = os.path.join(EVAL, "results")
SYSTEMS = ("bank_only", "bank_realistic", "shield", "standalone", "submitted_design")
SCENARIOS = ("S1", "S2", "S3", "S4", "S4b", "S5", "S6")
RF_SYSTEMS = ("shield", "standalone", "submitted_design")


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip() or "uncommitted"
    except (OSError, subprocess.TimeoutExpired):
        return "uncommitted"


def load_runs(results_dir: str = RESULTS) -> list:
    runs = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*__*__*.json"))):
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
        r["_file"] = os.path.relpath(path, REPO).replace(os.sep, "/")
        runs.append(r)
    return runs


def _agg(values: list, how):
    vals = [v for v in values if v is not None and not isinstance(v, bool)]
    bools = [v for v in values if isinstance(v, bool)]
    if bools and not vals:
        return all(bools) if how is statistics.median else (min(bools) if how is min else max(bools))
    return how(vals) if vals else None


def targets(scenario: str, system: str, med: dict, matrix_s: dict) -> dict:
    """Targets from prd/02 section 4 and team/SHARED.md; pass is None when the comparison can't be made."""
    out = {}

    def put(key, text, ok):
        out[key] = {"target": text, "pass": ok}

    if system not in RF_SYSTEMS:
        return out
    if scenario == "S4" and system == "shield":
        base = (matrix_s.get("bank_only") or {}).get("median", {}).get("fraud_value_through")
        v = med.get("fraud_value_through")
        put("fraud_value_through", "at least 80% below bank alone",
            None if base in (None, 0) or v is None else v <= 0.2 * base)
    if system == "shield":
        put("fail_open_value", "0", None if med.get("fail_open_value") is None else med["fail_open_value"] == 0)
        if med.get("deadline_miss_rate") is not None:
            put("deadline_miss_rate", "at most 0.1%", med["deadline_miss_rate"] <= 0.001)
        if scenario in ("S1", "S2", "S3") and med.get("legit_completed_rate") is not None:
            put("legit_completed_rate", "at least 99%", med["legit_completed_rate"] >= 0.99)
    if scenario in ("S1", "S2") and med.get("legit_held_per_1000") is not None:
        put("legit_held_per_1000", "at most 1", med["legit_held_per_1000"] <= 1)
    if med.get("legit_blocked_per_1000") is not None:
        put("legit_blocked_per_1000", "at most 1", med["legit_blocked_per_1000"] <= 1)
    if scenario == "S1" and med.get("p99_ms") is not None:
        put("p99_ms", "at most 50 ms", med["p99_ms"] <= 50)
    if scenario == "S1" and system == "shield" and med.get("shield_overhead_ms") is not None:
        ref = (matrix_s.get("shield") or {}).get("overhead_reference", "bank_only")
        put("shield_overhead_ms", f"at most 10 ms (against {ref})", med["shield_overhead_ms"] <= 10)
    if scenario == "S3":
        v = med.get("time_to_detect_s")
        put("time_to_detect_s", "at most 10 s", None if v is None else v <= 10)
    if scenario == "S2":
        put("false_coordinated_s", "0", None if med.get("false_coordinated_s") is None else med["false_coordinated_s"] == 0)
    if med.get("ledger_ok") is not None:
        put("ledger_ok", "valid", bool(med["ledger_ok"]))
    if med.get("reconcile_mismatch") is not None:
        put("reconcile_mismatch", "0", med["reconcile_mismatch"] == 0)
    return out


def build(results_dir: str = RESULTS, model_report: str = os.path.join(REPO, "scoring", "report.json")) -> dict:
    runs = load_runs(results_dir)
    matrix: dict = {}
    host = arch = None
    for scen in SCENARIOS:
        for system in SYSTEMS:
            cell_runs = [r for r in runs if r.get("scenario") == scen and r.get("system") == system]
            valid = [r for r in cell_runs if r.get("valid")]
            if not cell_runs:
                continue
            flat = [flatten(r) for r in valid]
            keys = sorted({k for f in flat for k in f})
            cell = {"median": {k: _agg([f.get(k) for f in flat], statistics.median) for k in keys},
                    "min": {k: _agg([f.get(k) for f in flat], min) for k in keys},
                    "max": {k: _agg([f.get(k) for f in flat], max) for k in keys},
                    "runs": [r["run_id"] for r in cell_runs], "valid_runs": len(valid),
                    "invalid": [{"run_id": r["run_id"], "reason": r.get("invalid_reason")} for r in cell_runs if not r.get("valid")],
                    "command": valid[0]["command"] if valid else cell_runs[0].get("command"),
                    "files": [r["_file"] for r in cell_runs]}
            matrix.setdefault(scen, {})[system] = cell
            host, arch = host or cell_runs[0].get("host"), arch or cell_runs[0].get("arch")
    s1 = matrix.get("S1", {})
    ref = "bank_realistic" if s1.get("bank_realistic") else "bank_only"  # the fair baseline when it was run
    if s1.get("shield") and s1.get(ref):
        a, b = s1[ref]["median"].get("p99_ms"), s1["shield"]["median"].get("p99_ms")
        if a is not None and b is not None:
            s1["shield"]["median"]["shield_overhead_ms"] = round(b - a, 3)
            s1["shield"]["overhead_reference"] = ref
    for scen, cells in matrix.items():
        for system, cell in cells.items():
            cell["targets"] = targets(scen, system, cell["median"], cells)
    cap_runs = [r for r in runs if r.get("scenario") == "CAP" and r.get("valid")]
    capacity = cap_runs[-1].get("capacity") if cap_runs else None
    model = None
    if os.path.exists(model_report):
        with open(model_report, encoding="utf-8") as fh:
            model = dict(json.load(fh), source="scoring/report.json")
    nnpa = any(r.get("nnpa") for r in runs)
    return {"contract": 2, "generated_ts": time.time(), "commit": _commit(), "host": host, "arch": arch, "nnpa": nnpa,
            "matrix": matrix, "capacity": capacity, "model": model,
            "command": "python -m eval.scenarios.summary"}


LABELS = [("shield_score", "Shield score"), ("protection", "Protection"), ("customer", "Customer"), ("timeliness", "Timeliness"),
          ("integrity", "Integrity"), ("fraud_value_sent", "M-01 Fraud value sent"), ("fraud_value_through", "M-02 Fraud value posted"),
          ("fail_open_value", "M-04 Bank fallback value"), ("attack_shed_share", "M-05 Attack shed"), ("legit_completed_rate", "M-07 Legit completed"),
          ("legit_held_per_1000", "M-08 Legit held per 1,000"), ("legit_blocked_per_1000", "M-09 Legit blocked per 1,000"),
          ("p99_ms", "M-11 Client p99 (ms)"), ("deadline_miss_rate", "M-12 Deadline misses"), ("shield_overhead_ms", "M-13 Shield overhead (ms)"),
          ("bank_engine_p99_ms", "M-14 Bank engine p99 (ms)"), ("stand_in_count", "M-16 Stand-in"), ("time_to_detect_s", "M-17 Time to detect (s)"),
          ("false_coordinated_s", "M-18 False COORDINATED (s)"), ("ledger_ok", "M-19 Ledger"), ("reconcile_mismatch", "M-20 Reconcile mismatches")]


def _fmt(v) -> str:
    if v is None:
        return "not run"
    if isinstance(v, bool):
        return "valid" if v else "BROKEN"
    if isinstance(v, float):
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.1f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def markdown(summary: dict, quantum_summary: str = os.path.join(REPO, "quantum", "results", "summary.json")) -> str:
    lines = ["# Results", "",
             f"Generated by `{summary['command']}` at commit `{summary['commit']}` on `{summary['host']}` ({summary['arch']}). "
             "Synthetic data. Every cell is the median of valid runs; the files are listed under each table.", ""]
    for scen, cells in summary["matrix"].items():
        systems = [s for s in SYSTEMS if s in cells]
        lines += [f"## {scen}", "", "| Metric | " + " | ".join(systems) + " |", "|---|" + "---|" * len(systems)]
        for key, label in LABELS:
            row = []
            for s in systems:
                med = cells[s]["median"].get(key)
                t = cells[s].get("targets", {}).get(key)
                mark = "" if not t or t["pass"] is None else (" (pass)" if t["pass"] else " (FAIL)")
                row.append(_fmt(med) + mark)
            if any(c != "not run" for c in row):
                lines.append(f"| {label} | " + " | ".join(row) + " |")
        for s in systems:
            c = cells[s]
            lines.append(f"\n- `{s}`: {c['valid_runs']} valid of {len(c['runs'])} runs ({', '.join(c['runs'])}); command `{c['command']}`")
            for bad in c.get("invalid", []):
                lines.append(f"  - invalid `{bad['run_id']}`: {bad['reason']}")
        lines.append("")
    cap = summary.get("capacity")
    if cap:
        lines += ["## Capacity", "", f"At {cap['rps']:,} requests/s with {int(100 * cap['attack_share'])}% attack: "
                  f"score everything needs {cap['cores_score_all']:.1f} cores, the cascade {cap['cores_cascade']:.1f}. "
                  f"CPU µs per request: {cap.get('cpu_us_per_req')}. Command `{cap.get('command')}`.", ""]
    if os.path.exists(quantum_summary):
        with open(quantum_summary, encoding="utf-8") as fh:
            q = json.load(fh)
        lines += ["## Quantum challenger", "", f"Verdict: **{q['verdict']}**", "", q.get("statement", ""), "",
                  "Details: `quantum/GATE.md` and `quantum/results/`.", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    summary = build()
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    with open(os.path.join(EVAL, "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(summary))
    print(f"summary: {sum(len(c) for c in summary['matrix'].values())} cells -> eval/results/summary.json, eval/RESULTS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
