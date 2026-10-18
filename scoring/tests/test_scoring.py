"""Scoring tests on SYNTHETIC data: features (no leakage, parity), the model interface, the contract, policy tuning,
the export schema, and the real handoff — exporter output fed into the quantum gate's `tune`."""
import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

import scoring
from scoring import data as datamod
from scoring import pipeline
from scoring.features import FEATURE_NAMES, GraphState, feature_hash
from scoring.model import Model
from scoring.tests import synth

SAMPLE = {"txn_id": "t-000999001", "ts": synth.START + 100.0, "from_acct": "cust-0001", "to_acct": "merch-0007",
          "amount": 120.0, "currency": "USD", "format": "chip"}
SIGNALS = {"cluster": None, "client_rate": None, "edge_action": None}


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    d = tmp_path_factory.mktemp("scoring")
    src = str(d / "synth.csv")
    synth.write(src)
    model_dir = str(d / "models")
    report = pipeline.train("replay", src, model_dir, policy_path=str(d / "policy.json"),
                            report_path=str(d / "report.json"))
    return {"src": src, "model_dir": model_dir, "report": report, "dir": str(d)}


def test_features_have_no_leakage_and_are_deterministic():
    df = datamod.read("replay", _small_csv())
    a = pipeline.build_features(df)
    b = pipeline.build_features(df)
    assert np.array_equal(a, b)                                   # deterministic
    # the very first payment of a fresh account has empty graph features
    first = df.iloc[0]
    g = GraphState()
    f0 = g.features(first["from_acct"], first["to_acct"], first["amount"], first["ts"])
    assert f0["f_source_out_cnt"] == 0 and f0["f_source_out_deg"] == 0 and f0["f_new_counterparty"] == 1.0
    # a fan-out raises fan-out degree and scatter, and a reciprocal payment sets the cycle flag
    g.add("A", "B", 10.0, 1.0)
    g.add("A", "C", 10.0, 2.0)
    f = g.features("A", "D", 10.0, 3.0)
    assert f["f_source_out_cnt"] == 2 and f["f_source_out_deg"] == 2 and f["f_scatter"] > 0
    # A paid B earlier, so a B->A payment is a reciprocal 2-cycle (the target A has paid the source B)
    assert g.features("B", "A", 5.0, 4.0)["f_cycle"] == 1.0
    assert g.features("Z", "Y", 5.0, 4.0)["f_cycle"] == 0.0        # unrelated accounts: no cycle


def _small_csv():
    path = os.path.join(tempfile.mkdtemp(), "small.csv")
    synth.write(path, days=6, per_day=40, bursts_per_day=2, legit_bursts_per_day=2, burst_size=6)
    return path


def test_model_interface_roundtrips(tmp_path):
    rng = np.random.default_rng(0)
    X = rng.random((400, len(FEATURE_NAMES)))
    y = (X[:, 0] + X[:, 11] > 1.0).astype(int)
    m = Model.make("gfp-gbm-v1", FEATURE_NAMES).fit(X, y)
    m.save(str(tmp_path))
    loaded = Model.load(str(tmp_path), "gfp-gbm-v1")
    assert loaded.features == FEATURE_NAMES and loaded.backend.name == "sklearn"
    assert np.allclose(m.predict(X), loaded.predict(X))
    assert json.load(open(tmp_path / "gfp-gbm-v1.model.json"))["backend"] == "sklearn"


def test_score_matches_the_contract(trained):
    scoring.load(trained["model_dir"])
    out = scoring.score(dict(SAMPLE), SIGNALS, 40)
    assert set(out) == {"score", "reasons", "model", "feature_hash", "latency_ms", "features"}
    assert 0.0 <= out["score"] <= 1.0 and out["model"] == "gfp-gbm-v1"
    assert isinstance(out["reasons"], list) and len(out["reasons"]) <= 5
    assert list(out["features"]) == FEATURE_NAMES and len(out["feature_hash"]) == 16


def test_unavailable_before_load_and_ingest_updates_the_graph(trained):
    fresh = scoring._Engine()
    with pytest.raises(scoring.Unavailable):
        fresh.score(dict(SAMPLE), 40)
    fresh.load(trained["model_dir"])
    # ingest() must update the graph: after a fan-out burst, the account's fan-out features rise
    mule = {"txn_id": "t-000999100", "ts": synth.START + 500.0, "from_acct": "mule-x", "to_acct": "drop-0",
            "amount": 2000.0, "currency": "USD", "format": "online"}
    before = fresh.score(dict(mule), 40)["features"]
    for j in range(12):
        fresh.ingest({"txn_id": f"i{j}", "ts": synth.START + 400.0 + j, "from_acct": "mule-x",
                      "to_acct": f"drop-{j}", "amount": 1500.0})
    after = fresh.score(dict(mule, txn_id="t-000999101", ts=synth.START + 600.0), 40)["features"]
    assert before["f_source_out_deg"] == 0 and after["f_source_out_deg"] >= 12


def test_policy_is_tuned_to_the_hold_cap(trained):
    policy = json.load(open(os.path.join(trained["dir"], "policy.json")))
    assert policy["version"] == "pol-v1" and policy["verify_at"] <= policy["hold_at"]
    df = datamod.add_split(datamod.read("replay", trained["src"]))
    X = pipeline.build_features(df)
    gfp = Model.load(trained["model_dir"], "gfp-gbm-v1")
    val_scores = gfp.predict(X[(df["split"] == "val").to_numpy()])
    held = float((val_scores >= policy["hold_at"]).mean())
    assert held <= 0.05                                           # ~2% cap, with headroom for a small sample


def test_export_writes_exactly_what_the_gate_reads(trained):
    out_dir = os.path.join(trained["dir"], "features")
    summary = pipeline.export("replay", trained["src"], out_dir, trained["model_dir"])
    cand = pd.read_csv(os.path.join(out_dir, "qml_candidates.csv"))
    need = ["txn_id", "ts", "day", "split", "label", "score", "from_acct"]
    assert list(cand.columns) == need + FEATURE_NAMES
    assert set(cand["split"]) <= {"val", "test"} and set(cand["label"]) <= {0, 1}
    assert cand[cand.split == "val"]["ts"].max() <= cand[cand.split == "test"]["ts"].min()
    assert summary["val_candidates"] > 0 and summary["test_candidates"] > 0
    cap = pd.read_csv(os.path.join(out_dir, "qml_capacity.csv"))
    assert list(cap.columns) == ["day", "transactions", "capacity", "positives"] and cap["positives"].sum() > 0


def test_export_output_feeds_the_quantum_gate(trained, tmp_path):
    """The real handoff: exporter output must run through the quantum gate's `tune` without error.

    Run as a subprocess, not an import, so the payment path (and `scoring/`) never imports `quantum` — the isolation
    check C-15 and the quantum↔scoring no-import rule stay strict (guardrail #5 and CONTRACTS).
    """
    import subprocess
    import sys

    out_dir = os.path.join(trained["dir"], "features")
    cand = os.path.join(out_dir, "qml_candidates.csv")
    cap = os.path.join(out_dir, "qml_capacity.csv")
    if not os.path.exists(cand):
        pipeline.export("replay", trained["src"], out_dir, trained["model_dir"])
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    results = tmp_path / "qresults"
    env = dict(os.environ, RF_QUANTUM_RESULTS=str(results), PYTHONPATH=repo)
    done = subprocess.run([sys.executable, "-m", "quantum.research.evaluate", "tune", "--candidates", cand,
                           "--capacity", cap], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    tuning = json.load(open(results / "tuning.json"))
    assert set(tuning["seeds"]) == {"1", "2", "3"} and tuning["inputs"][0]["path"] == cand


def test_feature_hash_is_stable():
    g = GraphState()
    f = g.features("A", "B", 100.0, 1.0)
    assert feature_hash(f) == feature_hash(dict(f))
