"""The decision rule and GATE.md (prd/05 sections 3.4, 3.5 and Part 7). The verdict is computed, never written by hand."""
from __future__ import annotations

import datetime as dt

MIN_GAIN = 0.02
SANITY_TOLERANCE = 0.03
MAX_WORKER_P99_MS = 1000.0

NO_ADVANTAGE = ("No advantage found. On HI-Small, our 8-qubit quantum challenger did not beat classical controls with "
                "the same inputs (details and intervals in RESULTS.md).")
PASS_TEMPLATE = ("On HI-Small, our 8-qubit quantum feature map, simulated exactly on LinuxONE, beat matched classical "
                 "controls by {gain:.1f} points of recall at analyst capacity (95% interval {lo:.1f} to {hi:.1f}, {k} of {k} "
                 "seeds). This is a useful feature map for this data, not a quantum computational advantage: the circuit "
                 "is small enough to simulate classically.")


def beats(rows: list) -> bool:
    """rows: one comparison per seed, each with diff and ci95. All seeds must gain >= 2 points with CI above 0."""
    return bool(rows) and all(r["diff"] >= MIN_GAIN and r["ci95"][0] > 0 for r in rows)


def verdict(qp1_vs_k4: list, qk1_vs_k1: list, n1_recall: list, random_recall: float, protocol_commit: str,
            worker_p99_ms=None) -> dict:
    sane = all(abs(x - random_recall) <= SANITY_TOLERANCE for x in n1_recall) if n1_recall else False
    committed = bool(protocol_commit) and protocol_commit != "UNCOMMITTED"
    qp1, qk1 = beats(qp1_vs_k4), beats(qk1_vs_k1)
    if not sane:
        label = "VOID (negative control failed: find the bug before reading any result)"
    elif not committed:
        label = "VOID (protocol not committed before testing)"
    elif qp1:
        label = "PASS"
    else:
        label = "NO ADVANTAGE FOUND"
    rerank = label == "PASS" and worker_p99_ms is not None and worker_p99_ms <= MAX_WORKER_P99_MS
    if label == "PASS":
        gains = [r["diff"] for r in qp1_vs_k4]
        statement = PASS_TEMPLATE.format(gain=100 * min(gains), lo=100 * min(r["ci95"][0] for r in qp1_vs_k4),
                                         hi=100 * max(r["ci95"][1] for r in qp1_vs_k4), k=len(qp1_vs_k4))
    elif label == "NO ADVANTAGE FOUND":
        statement = NO_ADVANTAGE
    else:
        statement = "No statement: the run is void."
    return {"verdict": label, "qp1_beats": qp1, "qk1_beats": qk1, "sanity_ok": sane, "protocol_committed": committed,
            "rerank_allowed": rerank, "statement": statement}


def gate_markdown(v: dict, qp1_vs_k4: list, qk1_vs_k1: list, n1_recall: list, random_recall: float,
                  protocol_commit: str, inputs: list, worker: dict) -> str:
    fmt = lambda rows: "; ".join(f"seed {r['seed']}: {100 * r['diff']:+.2f} pts [{100 * r['ci95'][0]:+.2f}, {100 * r['ci95'][1]:+.2f}]"
                                 for r in rows) or "not run"
    lines = [
        f"QML gate: {v['verdict']}",
        f"Generated: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `python -m quantum.research.evaluate test`",
        f"Protocol: quantum/PROTOCOL.md at commit {protocol_commit}",
        "Inputs: " + ", ".join(f"{i['path']} (sha256 {i['sha256'][:12]})" for i in inputs),
        f"QP-1 vs K4 recall at capacity, by seed: {fmt(qp1_vs_k4)}",
        f"QK-1 vs K1 recall at capacity, by seed: {fmt(qk1_vs_k1)}",
        f"N1 sanity: {', '.join(f'{x:.4f}' for x in n1_recall)} vs random {random_recall:.4f}: {'OK' if v['sanity_ok'] else 'VOID'}",
        f"Live worker p99: {worker.get('p99_ms', 'not measured')} ms on {worker.get('host', '?')} ({worker.get('backend', '?')})",
        f"RF_QML_MODE allowed: {'rerank' if v['rerank_allowed'] else 'shadow only'}",
        f"Statement: {v['statement']}",
        "",
        "Rule (prd/05 section 3.4): QP-1 beats classical only if, for every seed, it gains at least 2 points of recall at",
        "capacity over K4 and the 95% paired account-bootstrap interval is above 0. Even a PASS is not a quantum advantage.",
    ]
    return "\n".join(lines) + "\n"
