"""python -m quantum.research.evaluate <command>   (prd/05 Part 4)

  checks   Q-02 unit checks of the circuit, including numpy against Qiskit to 1e-9
  toy      Q-03 two-blob toy problem: QK-1 and K1 must both separate it
  tune     Q-05 tuning on validation only, 9 configurations per matched model, 3 seeds -> quantum/results/tuning.json
  freeze   Q-06 writes quantum/PROTOCOL.md from tuning.json (review it, then commit it before `test`)
  seal     on a machine without git: records PROTOCOL.md's SHA-256 and the time, in place of the commit
  test     Q-07 test-split evaluation under the committed protocol -> results, summary.json, GATE.md, models/
  bench    worker latency on this machine -> quantum/results/worker_latency.json (run on the VM)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time

import numpy as np

from quantum.core import artifact, circuit
from quantum.core.preprocess import Preprocessor
from quantum.research import gate, metrics
from quantum.research.models import MODELS, TUNED, _lr, build, grid

HERE = os.path.dirname(os.path.abspath(__file__))
QDIR = os.path.dirname(HERE)
REPO = os.path.dirname(QDIR)
# RF_QUANTUM_RESULTS lets a caller (A's handoff test) redirect tuning.json to a temp dir, so it never writes the repo
RESULTS = os.environ.get("RF_QUANTUM_RESULTS") or os.path.join(QDIR, "results")
MODELS_DIR = os.path.join(QDIR, "models")
PROTOCOL = os.path.join(QDIR, "PROTOCOL.md")
SEEDS = (1, 2, 3)
NEG_PER_POS = 4
N_BOOT = 2000
COMPARISONS = (("QP-1", "K4"), ("QK-1", "K1"), ("QP-1a", "K2"))
N1_SHUFFLES = 20


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _commit() -> str:
    return _git("rev-parse", "--short", "HEAD") or "uncommitted"


def _seal_path() -> str:
    return os.path.splitext(PROTOCOL)[0] + ".seal.json"


def protocol_commit() -> str:
    """The commit that last changed PROTOCOL.md, or UNCOMMITTED if it has local changes or was never committed.
    Without git (the LinuxONE VM), a seal written by `seal` stands in for the commit: `sealed-<sha256[:12]>`, valid
    only while PROTOCOL.md still has the sealed hash."""
    rel = os.path.relpath(PROTOCOL, REPO).replace(os.sep, "/")
    last = _git("log", "-1", "--format=%H", "--", rel)
    dirty = _git("status", "--porcelain", "--", rel)
    if last and not dirty:
        return last[:12]
    if os.path.exists(_seal_path()) and os.path.exists(PROTOCOL):
        with open(_seal_path(), encoding="utf-8") as fh:
            seal = json.load(fh)
        if seal.get("sha256") == _sha256(PROTOCOL):
            return "sealed-" + seal["sha256"][:12]
    return "UNCOMMITTED"


# ---------------- data ----------------
class Data:
    def __init__(self, candidates: str, capacity: str):
        import pandas as pd

        df = pd.read_csv(candidates)
        need = {"txn_id", "ts", "day", "split", "label", "score", "from_acct"}
        if not need <= set(df.columns):
            raise ValueError(f"{candidates} lacks columns {sorted(need - set(df.columns))}")
        self.fcols = [c for c in df.columns if c.startswith("f_")]
        if len(self.fcols) < circuit.N_QUBITS:
            raise ValueError(f"need at least {circuit.N_QUBITS} f_ columns, got {len(self.fcols)}")
        if not set(df["split"]) <= {"val", "test"}:
            raise ValueError("split must be val or test only")
        if not set(df["label"].unique()) <= {0, 1}:
            raise ValueError("label must be 0 or 1")
        df = df.sort_values("ts", kind="stable").reset_index(drop=True)
        val, test = df[df.split == "val"], df[df.split == "test"]
        if len(val) == 0 or len(test) == 0:
            raise ValueError("need both val and test rows")
        if val.ts.max() > test.ts.min():
            raise ValueError("every validation row must come before every test row")
        cap = pd.read_csv(capacity)
        if not {"day", "transactions", "capacity", "positives"} <= set(cap.columns):
            raise ValueError(f"{capacity} needs day,transactions,capacity,positives")
        self.val, self.test = val.reset_index(drop=True), test.reset_index(drop=True)
        self.capacity_by_day = dict(zip(cap["day"].astype(str), cap["capacity"].astype(int)))
        self.total_positives = int(cap["positives"].sum())
        self.inputs = [{"path": candidates, "sha256": _sha256(candidates)}, {"path": capacity, "sha256": _sha256(capacity)}]

    def X(self, df):
        return df[self.fcols].to_numpy(dtype=float)


def subsample(df, seed: int):
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(df["label"].to_numpy() == 1)
    neg = np.flatnonzero(df["label"].to_numpy() == 0)
    take = rng.choice(neg, size=min(len(neg), NEG_PER_POS * max(len(pos), 1)), replace=False)
    return df.iloc[np.sort(np.concatenate([pos, take]))]


# ---------------- commands ----------------
def cmd_checks(_args) -> int:
    rng = np.random.default_rng(0)
    u = rng.random((200, circuit.N_QUBITS))
    results = []
    psi = circuit.statevectors(u, 0.5)
    results.append(("state norm is 1", np.allclose(np.linalg.norm(psi, axis=1), 1, atol=1e-12)))
    K = circuit.kernel(psi, psi)
    results.append(("K(x, x) = 1", np.allclose(np.diag(K), 1, atol=1e-12)))
    results.append(("K symmetric", np.allclose(K, K.T, atol=1e-12)))
    results.append(("K positive semi-definite", np.linalg.eigvalsh((K + K.T) / 2).min() > -1e-9))
    phi = circuit.projected_features(u, 0.5)
    results.append(("<Z> and <ZZ> in [-1, 1]", bool(np.all(np.abs(phi) <= 1 + 1e-12))))
    q = circuit.qiskit_statevectors(u[:100], 0.5)
    if q is None:
        results.append(("numpy vs Qiskit to 1e-9 (Qiskit not installed: SKIPPED)", True))
        diff = None
    else:
        diff = float(np.abs(q - psi[:100]).max())
        results.append((f"numpy vs Qiskit to 1e-9 (max diff {diff:.2e})", diff < 1e-9))
    ok = True
    for name, passed in results:
        print(("PASS" if passed else "FAIL") + " Q-02 " + name)
        ok &= bool(passed)
    return 0 if ok else 1


def toy_problem(seed: int = 0):
    rng = np.random.default_rng(seed)
    n = 200
    a = np.clip(rng.normal(0.35, 0.08, (n, circuit.N_QUBITS)), 0, 1)
    b = np.clip(rng.normal(0.65, 0.08, (n, circuit.N_QUBITS)), 0, 1)
    U, y = np.vstack([a, b]), np.array([0] * n + [1] * n)
    idx = rng.permutation(len(y))
    return U[idx[:300]], y[idx[:300]], U[idx[300:]], y[idx[300:]]


def cmd_toy(_args) -> int:
    Utr, ytr, Ute, yte = toy_problem()
    s0 = np.zeros(len(ytr))
    ok = True
    for name, cfg in (("QK-1", {"lam": 1.0, "C": 1.0}), ("K1", {"gamma": 1.0, "C": 1.0})):
        m = build(name, cfg).fit(Utr, s0, ytr)
        acc = float(((m.score(Ute, np.zeros(len(yte))) > 0).astype(int) == yte).mean())
        passed = acc > 0.9
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'} Q-03 {name} toy accuracy {acc:.3f}")
    return 0 if ok else 1


def _split_val(val):
    cut = int(len(val) * 0.7)
    return val.iloc[:cut], val.iloc[cut:]


def cmd_tune(args) -> int:
    data = Data(args.candidates, args.capacity)
    fit_part, sel_part = _split_val(data.val)
    out = {"generated": _stamp(), "inputs": data.inputs, "seeds": {}}
    for seed in SEEDS:
        pre = Preprocessor().fit(data.X(fit_part), data.fcols)
        sub = subsample(fit_part, seed)
        Usub, ssub, ysub = pre.transform(data.X(sub)), sub["score"].to_numpy(), sub["label"].to_numpy()
        Usel, ssel, ysel = pre.transform(data.X(sel_part)), sel_part["score"].to_numpy(), sel_part["label"].to_numpy()
        per = {}
        for name in TUNED:
            scores = []
            for cfg in grid(name):
                m = build(name, cfg, seed).fit(Usub, ssub, ysub)
                scores.append({"config": cfg, "val_pr_auc": metrics.pr_auc(ysel, m.score(Usel, ssel))})
            best = max(scores, key=lambda r: (np.nan_to_num(r["val_pr_auc"], nan=-1.0)))
            if name == "QK-1":
                off = circuit.kernel(*(2 * [circuit.statevectors(Usub[:300], best["config"]["lam"])]))
                spread = float(off[~np.eye(len(off), dtype=bool)].std())
                best = dict(best, kernel_offdiag_std=spread, kernel_flat=spread < 0.01)
            per[name] = {"best": best["config"], "best_val_pr_auc": best["val_pr_auc"], "all": scores,
                         **({"kernel_offdiag_std": best["kernel_offdiag_std"], "kernel_flat": best["kernel_flat"]}
                            if name == "QK-1" else {})}
            print(f"seed {seed} {name}: best {best['config']} val PR-AUC {best['val_pr_auc']:.4f}")
        out["seeds"][str(seed)] = per
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "tuning.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {path}. Next: `freeze`, review quantum/PROTOCOL.md, commit it, then `test`.")
    return 0


PROTOCOL_HEAD = """# Quantum challenger protocol (pre-registered; prd/05 Part 3)

This file is written by `python -m quantum.research.evaluate freeze`. C reviews it and commits it **before** any
test-split evaluation. The commit time is the proof. `test` refuses a protocol with local changes, and the gate marks
results VOID if the protocol was not committed. Any later change is a new protocol version with its reason below.

- Candidates: the top 2% of each day's transactions by the `gfp-gbm-v1` score (A's export, CONTRACTS section 13).
- Capacity: each test day, 1% of that day's transactions, taken from the top of the ranking within the candidates.
- Primary metric: recall at capacity (laundering inside the daily capacity slices / all laundering in the test period).
- Training: validation candidates; all positives plus up to 4 times as many negatives, seeds 1, 2, 3.
- Tuning: validation only, first 70% fit, last 30% select by PR-AUC; 9 configurations per matched model.
- Statistics: paired bootstrap over sender accounts, 2,000 resamples, 95% percentile interval.
- Rule: QP-1 beats classical only if, for every seed, recall at capacity improves on K4 by at least 2 points and the
  interval's lower end is above 0. Same rule for QK-1 against K1. N1 (QP-1 on shuffled labels, averaged over 20
  shuffles) must land within 3 points of a random ordering, or every result is void.
- Even a PASS is not a quantum computational advantage: 8 qubits are simulated exactly on a classical machine.

## Frozen choices
```json
"""


def cmd_freeze(_args) -> int:
    path = os.path.join(RESULTS, "tuning.json")
    with open(path, encoding="utf-8") as fh:
        tuning = json.load(fh)
    frozen = {"protocol_version": 1, "qubits": circuit.N_QUBITS, "layers": circuit.LAYERS, "tau": circuit.TAU,
              "seeds": list(SEEDS), "negatives_per_positive": NEG_PER_POS, "n_boot": N_BOOT, "min_gain": gate.MIN_GAIN,
              "sanity_tolerance": gate.SANITY_TOLERANCE, "inputs": tuning["inputs"],
              "choices": {seed: {name: per["best"] for name, per in models.items()} for seed, models in tuning["seeds"].items()},
              "flat_kernel_warning": any(m.get("QK-1", {}).get("kernel_flat") for m in tuning["seeds"].values())}
    with open(PROTOCOL, "w", encoding="utf-8") as fh:
        fh.write(PROTOCOL_HEAD + json.dumps(frozen, indent=1) + "\n```\n\n## Changes\n(none)\n")
    if os.path.exists(_seal_path()):
        os.remove(_seal_path())   # a new protocol needs a new seal
    print(f"wrote {PROTOCOL}. Review it, `git add quantum/PROTOCOL.md && git commit`, post the hash, then run `test`.")
    return 0


def cmd_seal(_args) -> int:
    """For a machine without git: seal PROTOCOL.md before `test` by recording its SHA-256 and the time."""
    if not os.path.exists(PROTOCOL):
        print("refused: no quantum/PROTOCOL.md; run `freeze` first", file=sys.stderr)
        return 3
    seal = {"sha256": _sha256(PROTOCOL), "sealed_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "host": platform.node(), "note": "stands in for the git commit of PROTOCOL.md on a machine without git"}
    with open(_seal_path(), "w", encoding="utf-8") as fh:
        json.dump(seal, fh, indent=1)
    print(f"sealed quantum/PROTOCOL.md (sha256 {seal['sha256'][:12]}) at {seal['sealed_utc']}. Now run `test`.")
    return 0


def read_protocol() -> dict:
    with open(PROTOCOL, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not m:
        raise ValueError("PROTOCOL.md has no frozen JSON block; run `freeze` first")
    return json.loads(m.group(1))


def _result(model, seed, backend, data, rec, pr, prec, control, cmp_, ms, pcommit, cmdline) -> dict:
    return {"model": model, "seed": seed, "backend": backend, "shots": None, "inputs": "u8+s" if model in ("QP-1", "K4") else ("s" if model == "K3" else "u8"),
            "n_train": data["n_train"], "n_test": data["n_test"], "recall_at_cap": rec, "pr_auc": pr, "precision_at_cap": prec,
            "control": control, "diff_vs_control": cmp_["diff"] if cmp_ else None, "diff_ci95": cmp_["ci95"] if cmp_ else None,
            "ms_per_item": ms, "qpu_seconds": 0, "protocol_commit": pcommit, "command": cmdline, "commit": _commit()}


def cmd_test(args) -> int:
    proto = read_protocol()
    pcommit = protocol_commit()
    if pcommit == "UNCOMMITTED" and not args.allow_uncommitted:
        print("refused: quantum/PROTOCOL.md is not committed or sealed (or changed since). Commit it, or run "
              "`python -m quantum.research.evaluate seal` on a machine without git.", file=sys.stderr)
        return 3
    data = Data(args.candidates, args.capacity)
    for want, got in zip(proto["inputs"], data.inputs):
        if want["sha256"] != got["sha256"]:
            print(f"refused: {got['path']} changed since the protocol was frozen", file=sys.stderr)
            return 3
    test = data.test
    y, s, groups = test["label"].to_numpy(), test["score"].to_numpy(), test["from_acct"].astype(str).to_numpy()
    cap = metrics.Capacity(test["day"].astype(str).to_numpy(), data.capacity_by_day, data.total_positives)
    rand = metrics.random_order_recall(y, cap)
    stamp, cmdline = _stamp(), "python -m quantum.research.evaluate test " + " ".join(sys.argv[2:])
    os.makedirs(RESULTS, exist_ok=True)
    rows, comparisons, n1 = [], {a: [] for a, _ in COMPARISONS}, []
    for seed in proto["seeds"]:
        choices = proto["choices"][str(seed)]
        pre = Preprocessor().fit(data.X(data.val), data.fcols)
        sub = subsample(data.val, seed)
        Usub, ssub, ysub = pre.transform(data.X(sub)), sub["score"].to_numpy(), sub["label"].to_numpy()
        Ute = pre.transform(data.X(test))
        scores, fitted, timing = {}, {}, {}
        for name in list(TUNED) + ["K3"]:
            m = build(name, choices.get(name, {}), seed).fit(Usub, ssub, ysub)
            t0 = time.perf_counter()
            scores[name] = m.score(Ute, s)
            timing[name] = 1000 * (time.perf_counter() - t0) / max(len(y), 1)
            fitted[name] = m
        # N1: QP-1 on shuffled labels, averaged over N1_SHUFFLES shuffles (one shuffle can still point along the
        # informative classical score by chance; the average of random directions sits at a random ordering)
        base = build("QP-1", choices["QP-1"], seed)
        Xsub, Xte = base._X(Usub, ssub), base._X(Ute, s)
        rng_n1 = np.random.default_rng(1000 + seed)
        n1_recalls, n1_score = [], None
        for _ in range(N1_SHUFFLES):
            lr = _lr(choices["QP-1"]["C"]).fit(Xsub, rng_n1.permutation(ysub))
            sc_n1 = lr.decision_function(Xte)
            n1_score = sc_n1 if n1_score is None else n1_score
            n1_recalls.append(metrics.recall_at_capacity(y, sc_n1, cap))
        n1.append(float(np.mean(n1_recalls)))
        meta = {"n_train": int(len(ysub)), "n_test": int(len(y))}
        cmp_by_model = {}
        for a, b in COMPARISONS:
            res = metrics.paired_bootstrap(y, scores[a], scores[b], groups, cap, n_boot=proto["n_boot"], seed=seed)
            cmp_by_model[a] = (b, res)
            comparisons[a].append(dict(res, seed=seed))
        for name in list(TUNED) + ["K3", "N1"]:
            sc = n1_score if name == "N1" else scores[name]
            control, res = cmp_by_model.get(name, (None, None))
            backend = "numpy-statevector" if name.startswith("Q") or name == "N1" else "classical"
            rec = n1[-1] if name == "N1" else metrics.recall_at_capacity(y, sc, cap)
            row = _result(name, seed, backend, meta, rec, metrics.pr_auc(y, sc),
                          metrics.precision_at_capacity(y, sc, cap), control, res, timing.get(name), pcommit, cmdline)
            rows.append(row)
            with open(os.path.join(RESULTS, f"{name}__seed{seed}__{stamp}.json"), "w", encoding="utf-8") as fh:
                json.dump(row, fh, indent=1)
        if seed == proto["seeds"][0]:
            os.makedirs(MODELS_DIR, exist_ok=True)
            pre.save(os.path.join(MODELS_DIR, "preprocess-v1.json"))
            with open(os.path.join(MODELS_DIR, "circuit-v1.json"), "w", encoding="utf-8") as fh:
                json.dump({"version": "circuit-v1", "qubits": circuit.N_QUBITS, "layers": circuit.LAYERS, "tau": circuit.TAU,
                           "pairs": circuit.pairs(), "encoding": "RY(theta) then RZ(theta), theta = lam * pi * u"}, fh)
            artifact.save(fitted["QP-1"].artifact(), os.path.join(MODELS_DIR, "qp1-v1.json"))
            try:
                artifact.save(fitted["QK-1"].artifact(), os.path.join(MODELS_DIR, "qk1-v1.json"))
            except ValueError as exc:
                print(f"note: QK-1 artifact not saved ({exc})")
    worker = {}
    wpath = os.path.join(RESULTS, "worker_latency.json")
    if os.path.exists(wpath):
        with open(wpath, encoding="utf-8") as fh:
            worker = json.load(fh)
    v = gate.verdict(comparisons["QP-1"], comparisons["QK-1"], n1, rand, pcommit, worker.get("p99_ms"))
    with open(os.path.join(QDIR, "GATE.md"), "w", encoding="utf-8") as fh:
        fh.write(gate.gate_markdown(v, comparisons["QP-1"], comparisons["QK-1"], n1, rand, pcommit, data.inputs, worker))
    summary = {"generated_ts": time.time(), "verdict": v["verdict"], "statement": v["statement"],
               "rerank_allowed": v["rerank_allowed"], "random_order_recall": rand, "rows": rows, "gate_file": "quantum/GATE.md",
               # the gate's own checks, so the console shows why the verdict is what it is without parsing GATE.md
               "gate": {k: v[k] for k in ("qp1_beats", "qk1_beats", "sanity_ok", "protocol_committed")},
               "n1_recall": n1, "protocol_commit": pcommit, "min_gain": gate.MIN_GAIN,
               "sanity_tolerance": gate.SANITY_TOLERANCE, "worker": worker or None}
    with open(os.path.join(RESULTS, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    print(f"verdict: {v['verdict']}\n{v['statement']}")
    return 0


def cmd_bench(args) -> int:
    from quantum.worker.worker import load_challenger

    pre, ch = load_challenger(MODELS_DIR)
    rng = np.random.default_rng(0)
    times = []
    for _ in range(args.n):
        x = rng.normal(size=(1, len(pre.columns)))
        t0 = time.perf_counter()
        ch.score(pre.transform(x), np.array([0.9]))
        times.append(1000 * (time.perf_counter() - t0))
    times.sort()
    out = {"host": platform.node(), "arch": platform.machine(), "backend": "numpy-statevector", "n": args.n,
           "p50_ms": round(times[len(times) // 2], 3), "p99_ms": round(times[int(0.99 * (len(times) - 1))], 3),
           "model": ch.version, "measured": _stamp()}
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "worker_latency.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out))
    return 0


def main(argv=None) -> int:
    data = os.environ.get("RF_DATA", "/srv/riskfabric/data")
    ap = argparse.ArgumentParser(prog="quantum.research.evaluate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("checks")
    sub.add_parser("toy")
    for name in ("tune", "test"):
        p = sub.add_parser(name)
        p.add_argument("--candidates", default=os.path.join(data, "features", "qml_candidates.csv"))
        p.add_argument("--capacity", default=os.path.join(data, "features", "qml_capacity.csv"))
        if name == "test":
            p.add_argument("--allow-uncommitted", action="store_true", help="dry runs only: the gate will say VOID")
    sub.add_parser("freeze")
    sub.add_parser("seal")
    b = sub.add_parser("bench")
    b.add_argument("--n", type=int, default=500)
    args = ap.parse_args(argv)
    return {"checks": cmd_checks, "toy": cmd_toy, "tune": cmd_tune, "freeze": cmd_freeze, "seal": cmd_seal, "test": cmd_test,
            "bench": cmd_bench}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
