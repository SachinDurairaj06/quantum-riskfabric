"""Quantum challenger tests: circuit checks (Q-02), toy (Q-03), metrics, the gate rule, the full protocol pipeline
on synthetic data, and the shadow worker (C-15 parts 3 and 4)."""
import json
import os

import fakeredis
import numpy as np
import pandas as pd
import pytest

from quantum.core import circuit
from quantum.core.artifact import Challenger
from quantum.research import evaluate, gate, metrics
from quantum.worker.worker import RerankRefused, Worker


# ---------- circuit ----------
def test_circuit_unit_checks_pass():
    assert evaluate.cmd_checks(None) == 0


def test_numpy_matches_qiskit_to_1e9():
    u = np.random.default_rng(1).random((50, 8))
    q = circuit.qiskit_statevectors(u, 1.0)
    if q is None:
        pytest.skip("qiskit not installed")
    assert np.abs(q - circuit.statevectors(u, 1.0)).max() < 1e-9


def test_shots_converge_to_exact():
    u = np.random.default_rng(2).random((5, 8))
    exact = circuit.projected_features(u, 0.5)
    est = circuit.projected_from_shots(u, 0.5, shots=20000, seed=0)
    assert np.abs(est - exact).max() < 0.03 and exact.shape == (5, 15)


def test_toy_problem_is_separated():
    assert evaluate.cmd_toy(None) == 0


# ---------- metrics ----------
def test_recall_at_capacity_and_random_order():
    day = np.array(["d1"] * 4 + ["d2"] * 4)
    label = np.array([1, 0, 0, 1, 0, 1, 0, 0])
    score = np.array([0.9, 0.8, 0.1, 0.2, 0.7, 0.6, 0.5, 0.1])
    cap = metrics.Capacity(day, {"d1": 2, "d2": 1}, total_positives=4)
    assert metrics.recall_at_capacity(label, score, cap) == pytest.approx(1 / 4)
    assert metrics.recall_at_capacity(label, -score, cap) == pytest.approx(1 / 4)
    assert metrics.random_order_recall(label, cap) == pytest.approx((0.5 * 2 + 0.25 * 1) / 4)


def test_bootstrap_identical_models_has_zero_difference():
    rng = np.random.default_rng(0)
    label, score = (rng.random(400) < 0.1).astype(int), rng.random(400)
    day = np.repeat(["a", "b"], 200)
    cap = metrics.Capacity(day, {"a": 50, "b": 50}, int(label.sum()))
    res = metrics.paired_bootstrap(label, score, score, rng.integers(0, 80, 400), cap, n_boot=100)
    assert res["diff"] == 0 and res["ci95"] == [0.0, 0.0]


# ---------- gate ----------
def _rows(diffs, lo):
    return [{"seed": i + 1, "diff": d, "ci95": [lo, d + 0.01]} for i, d in enumerate(diffs)]


def test_gate_rule():
    ok = gate.verdict(_rows([0.03, 0.025, 0.04], 0.001), [], [0.2], 0.21, "abc123", 5.0)
    assert ok["verdict"] == "PASS" and ok["rerank_allowed"] and "not a quantum computational advantage" in ok["statement"]
    one_seed_short = gate.verdict(_rows([0.03, 0.01, 0.04], 0.001), [], [0.2], 0.21, "abc123", 5.0)
    assert one_seed_short["verdict"] == "NO ADVANTAGE FOUND" and one_seed_short["statement"] == gate.NO_ADVANTAGE
    ci_touches_zero = gate.verdict(_rows([0.03] * 3, 0.0), [], [0.2], 0.21, "abc123", 5.0)
    assert ci_touches_zero["verdict"] == "NO ADVANTAGE FOUND"
    assert gate.verdict(_rows([0.03] * 3, 0.01), [], [0.5], 0.21, "abc", 5.0)["verdict"].startswith("VOID")
    assert gate.verdict(_rows([0.03] * 3, 0.01), [], [0.2], 0.21, "UNCOMMITTED", 5.0)["verdict"].startswith("VOID")
    assert not gate.verdict(_rows([0.03] * 3, 0.01), [], [0.2], 0.21, "abc", None)["rerank_allowed"]


# ---------- the full protocol on synthetic data ----------
def synthetic(tmp_path, seed=0):
    rng = np.random.default_rng(seed)
    rows, cap_rows, txn = [], [], 0
    for d in range(7):
        split = "val" if d < 4 else "test"
        day = f"2022-09-{d + 1:02d}"
        n, n_pos = 400, 16
        label = np.zeros(n, int)
        label[rng.choice(n, n_pos, replace=False)] = 1
        f = rng.normal(size=(n, 12))
        f[:, :3] += 1.2 * label[:, None]
        score = np.clip(0.6 + 0.15 * label + 0.1 * rng.normal(size=n), 0, 1)
        for i in range(n):
            rows.append({"txn_id": f"t-{txn:09d}", "ts": 1660000000 + d * 86400 + i, "day": day, "split": split,
                         "label": int(label[i]), "score": float(score[i]), "from_acct": f"A{rng.integers(0, 300)}",
                         **{f"f_{j}": float(f[i, j]) for j in range(12)}})
            txn += 1
        if split == "test":
            cap_rows.append({"day": day, "transactions": 20000, "capacity": 200, "positives": n_pos + 4})
    cand, cap = tmp_path / "qml_candidates.csv", tmp_path / "qml_capacity.csv"
    pd.DataFrame(rows).to_csv(cand, index=False)
    pd.DataFrame(cap_rows).to_csv(cap, index=False)
    return str(cand), str(cap)


@pytest.fixture
def qdirs(tmp_path, monkeypatch):
    q = tmp_path / "q"
    for sub in ("results", "models"):
        (q / sub).mkdir(parents=True)
    monkeypatch.setattr(evaluate, "QDIR", str(q))
    monkeypatch.setattr(evaluate, "RESULTS", str(q / "results"))
    monkeypatch.setattr(evaluate, "MODELS_DIR", str(q / "models"))
    monkeypatch.setattr(evaluate, "PROTOCOL", str(q / "PROTOCOL.md"))
    monkeypatch.setattr(evaluate, "N_BOOT", 100)
    return q


def test_pipeline_tune_freeze_test_refuses_then_runs_void_when_uncommitted(tmp_path, qdirs):
    cand, cap = synthetic(tmp_path)
    assert evaluate.main(["tune", "--candidates", cand, "--capacity", cap]) == 0
    assert evaluate.main(["freeze"]) == 0
    proto = evaluate.read_protocol()
    assert set(proto["choices"]["1"]) >= {"QK-1", "QP-1", "K1", "K4"} and proto["n_boot"] == 100
    # not committed in git: test refuses without the explicit dry-run flag
    assert evaluate.main(["test", "--candidates", cand, "--capacity", cap]) == 3
    assert evaluate.main(["test", "--candidates", cand, "--capacity", cap, "--allow-uncommitted"]) == 0
    summary = json.loads((qdirs / "results" / "summary.json").read_text())
    assert summary["verdict"].startswith("VOID")
    models = {r["model"] for r in summary["rows"]}
    assert {"QK-1", "QP-1", "QP-1a", "K0", "K1", "K2", "K3", "K4", "N1"} <= models
    qp1 = [r for r in summary["rows"] if r["model"] == "QP-1"]
    assert len(qp1) == 3 and all(r["control"] == "K4" and len(r["diff_ci95"]) == 2 for r in qp1)
    assert all(r["protocol_commit"] == "UNCOMMITTED" and r["command"].startswith("python -m") for r in qp1)
    gate_text = (qdirs / "GATE.md").read_text()
    assert gate_text.startswith("QML gate: VOID") and "shadow only" in gate_text
    for name in ("preprocess-v1.json", "circuit-v1.json", "qp1-v1.json"):
        assert (qdirs / "models" / name).stat().st_size < 1_000_000


def test_changed_input_after_freeze_is_refused(tmp_path, qdirs):
    cand, cap = synthetic(tmp_path)
    evaluate.main(["tune", "--candidates", cand, "--capacity", cap])
    evaluate.main(["freeze"])
    with open(cand, "a") as fh:
        fh.write("\n")
    assert evaluate.main(["test", "--candidates", cand, "--capacity", cap, "--allow-uncommitted"]) == 3


# ---------- the shadow worker ----------
@pytest.fixture
def artifacts(tmp_path, qdirs):
    cand, cap = synthetic(tmp_path, seed=3)
    evaluate.main(["tune", "--candidates", cand, "--capacity", cap])
    evaluate.main(["freeze"])
    evaluate.main(["test", "--candidates", cand, "--capacity", cap, "--allow-uncommitted"])
    return qdirs


def test_worker_scores_holds_in_shadow_and_writes_ledger(artifacts):
    r = fakeredis.FakeRedis(decode_responses=True)
    w = Worker(r, str(artifacts / "models"), "shadow", str(artifacts / "GATE.md"))
    w.step(block_ms=1)
    feats = {f"f_{j}": 0.5 for j in range(12)}
    r.hset("hold:t-000000001", mapping={"score": "0.93", "features": json.dumps(feats)})
    r.xadd("decisions", {"txn_id": "t-000000001", "action": "HOLD"})
    r.xadd("decisions", {"txn_id": "t-000000002", "action": "ALLOW"})
    w.step(block_ms=1)
    q = r.hgetall("qml:t-000000001")
    assert q["status"] == "ok" and 0 <= float(q["score"]) <= 1 and q["backend"] == "numpy-statevector"
    assert not r.exists("qml:t-000000002") and not r.exists("hold:rank")
    rec = json.loads(r.xrange("ledger.in")[0][1]["rec"])
    assert rec["kind"] == "challenger" and rec["txn_id"] == "t-000000001"


def test_worker_marks_missing_features_after_two_seconds(artifacts):
    r = fakeredis.FakeRedis(decode_responses=True)
    now = [1000.0]
    w = Worker(r, str(artifacts / "models"), "shadow", str(artifacts / "GATE.md"), clock=lambda: now[0])
    w.step(block_ms=1)
    r.xadd("decisions", {"txn_id": "t-000000009", "action": "HOLD"})
    w.step(block_ms=1)
    assert not r.exists("qml:t-000000009")
    now[0] += 2.5
    w.step(block_ms=1)
    assert r.hget("qml:t-000000009", "status") == "no_features"


def test_rerank_refused_without_a_passing_gate(artifacts):
    r = fakeredis.FakeRedis(decode_responses=True)
    with pytest.raises(RerankRefused):
        Worker(r, str(artifacts / "models"), "rerank", str(artifacts / "GATE.md"))


def test_frozen_qp1_artifact_reproduces_research_scores(artifacts):
    ch = Challenger.load(str(artifacts / "models" / "qp1-v1.json"))
    u = np.random.default_rng(0).random((10, 8))
    out = ch.score(u, np.full(10, 0.7))
    assert out.shape == (10,) and np.all((0 <= out) & (out <= 1))


def test_no_quantum_imports_in_the_payment_path():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for folder in ("decision", "scoring", "bank"):
        base = os.path.join(repo, folder)
        for root, _, files in os.walk(base):
            for name in files:
                if name.endswith(".py"):
                    text = open(os.path.join(root, name), encoding="utf-8").read()
                    assert "qiskit" not in text and "import quantum" not in text and "from quantum" not in text, name


def test_seal_stands_in_for_the_commit_without_git(tmp_path, qdirs):
    cand, cap = synthetic(tmp_path)
    assert evaluate.main(["tune", "--candidates", cand, "--capacity", cap]) == 0
    assert evaluate.main(["freeze"]) == 0
    assert evaluate.main(["test", "--candidates", cand, "--capacity", cap]) == 3        # neither committed nor sealed
    assert evaluate.main(["seal"]) == 0
    assert evaluate.protocol_commit().startswith("sealed-")
    assert evaluate.main(["test", "--candidates", cand, "--capacity", cap]) == 0
    assert json.loads((qdirs / "results" / "summary.json").read_text())["gate"]["protocol_committed"] is True
    # editing the protocol after sealing breaks the seal, and a new freeze removes it
    with open(evaluate.PROTOCOL, "a", encoding="utf-8") as fh:
        fh.write("\nedited\n")
    assert evaluate.protocol_commit() == "UNCOMMITTED"
    assert evaluate.main(["freeze"]) == 0 and not os.path.exists(evaluate._seal_path())


def test_control_data_has_feature_map_structure(tmp_path):
    import pandas as pd

    from quantum.research import evaluate
    cand, cap = evaluate.control_data(str(tmp_path), rows=3000, seed=7)
    df = pd.read_csv(cand)
    assert {"txn_id", "ts", "day", "split", "label", "score", "from_acct"} <= set(df.columns)
    assert len([c for c in df.columns if c.startswith("f_")]) == evaluate.circuit.N_QUBITS
    assert set(df["label"].unique()) <= {0, 1} and 0.04 < df["label"].mean() < 0.16   # a minority of positives
    assert set(df["split"]) == {"val", "test"}
    capdf = pd.read_csv(cap)
    assert {"day", "transactions", "capacity", "positives"} <= set(capdf.columns) and capdf["positives"].sum() > 0


def test_control_labels_live_in_the_pipelines_input_frame(tmp_path):
    """Regression: the control's labels were computed in a PCA frame the pipeline never sees (standardised independent
    columns leave PCA with arbitrary axes), so even the true model could not learn them. The tuning slice (first 70% of
    validation) and the test fit (all of validation) must now give the same circuit inputs, and the projected quantum
    features in that frame must predict the label."""
    import pandas as pd
    from sklearn.linear_model import LogisticRegression

    from quantum.core import circuit
    from quantum.core.preprocess import Preprocessor
    from quantum.research import evaluate, metrics
    cand, _ = evaluate.control_data(str(tmp_path), rows=4000, seed=11, alpha=1.0)
    df = pd.read_csv(cand)
    cols = [c for c in df.columns if c.startswith("f_")]
    X, y, val = df[cols].to_numpy(), df["label"].to_numpy(), (df["split"] == "val").to_numpy()
    vi = np.flatnonzero(val)
    U_val = Preprocessor().fit(X[val], cols).transform(X)
    U_fit = Preprocessor().fit(X[vi[: int(len(vi) * 0.7)]], cols).transform(X)
    assert np.abs(U_val - U_fit).max() < 0.2
    phi = circuit.projected_features(U_val, 0.5)
    lr = LogisticRegression(max_iter=5000).fit(phi[val], y[val])
    assert metrics.pr_auc(y[~val], lr.decision_function(phi[~val])) > 0.8


def test_control_alpha_moves_from_basic_to_entangled(tmp_path):
    import pandas as pd

    from quantum.research import evaluate
    basic, _ = evaluate.control_data(str(tmp_path / "a0"), rows=2000, seed=3, alpha=0.0)
    ent, _ = evaluate.control_data(str(tmp_path / "a1"), rows=2000, seed=3, alpha=1.0)
    lb, le = pd.read_csv(basic)["label"].to_numpy(), pd.read_csv(ent)["label"].to_numpy()
    assert 0.06 < lb.mean() < 0.12 and 0.06 < le.mean() < 0.12
    assert (lb != le).mean() > 0.05                      # the two patterns flag different payments
    with pytest.raises(ValueError):
        evaluate.control_data(str(tmp_path / "bad"), rows=500, alpha=1.5)


def test_sweep_writes_matched_comparisons(tmp_path, monkeypatch):
    from quantum.research import evaluate
    monkeypatch.setattr(evaluate, "RESULTS", str(tmp_path))
    assert evaluate.main(["sweep", "--rows", "1500", "--alphas", "0", "1", "--n-boot", "50"]) == 0
    out = json.loads((tmp_path / "advantage_sweep.json").read_text())
    assert [p["alpha"] for p in out["points"]] == [0.0, 1.0]
    for p in out["points"]:
        assert set(p["comparisons"]) == {"QP-1", "QK-1"}
        assert len(p["comparisons"]["QP-1"]["by_seed"]) == len(evaluate.SEEDS)
        assert all(len(v) == len(evaluate.SEEDS) for v in p["recall_at_cap"].values())
    assert "NOT bank data" in out["label"]
