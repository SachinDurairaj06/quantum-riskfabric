"""Evaluation tests: metrics from synthetic run data, summary and targets, runner plan, capacity, and offline checks."""
import json
import os

import pytest

from eval.checks import run as checks
from eval.scenarios import metrics, runner, summary


def truth_line(i, kind, status=200, action="ALLOW", rtt=8.0, verified=None, amount=100.0, fp="fp-a", ts=1000.0):
    label = 1 if kind in ("launder", "cardtest") else 0
    return {"run_id": "r", "system": "shield", "req_id": f"q{i}", "txn_id": f"t-{i:09d}", "ts_sent": ts, "kind": kind,
            "label": label, "amount": amount, "fp": fp, "ip": "198.18.0.1", "asn": 64512, "status": status, "action": action,
            "rtt_ms": rtt, "verified": verified}


def test_metrics_from_a_small_run():
    truth = [truth_line(1, "legit"), truth_line(2, "legit", action="VERIFY", verified=True),
             truth_line(3, "legit", action="HOLD"), truth_line(4, "legit", status=429, action=None),
             truth_line(5, "launder", amount=5000.0), truth_line(6, "launder", action="HOLD", amount=7000.0),
             truth_line(7, "bot", status=403, action=None, fp="fp-bot"), truth_line(8, "bot", action="HOLD", fp="fp-bot"),
             truth_line(9, "legit", rtt=150.0), truth_line(10, "legit", ts=10.0)]   # last one is in the warm-up
    postings = [{"txn_id": f"t-{i:09d}", "amount": a} for i, a in ((1, 100.0), (2, 100.0), (5, 5000.0), (9, 100.0))]
    bank_log = [{"ts": "1000", "txn_id": "t-000000005", "entry": "channel", "timeout": "1", "posted": "1", "latency_ms": "120"},
                {"ts": "1000", "txn_id": "t-000000008", "entry": "engine", "timeout": "0", "latency_ms": "4"}]
    threat = [{"ts": "1001", "level": "COORDINATED", "clusters": json.dumps([{"key": "fp-bot", "action": "throttle"}])}]
    ledger = [{"kind": "shadow", "txn_id": "t-000000005", "shadow_score": 0.95}, {"kind": "shadow", "txn_id": "t-000000001", "shadow_score": 0.1}]
    m = metrics.compute(truth, postings, bank_log, [], threat, ledger, True, {}, "shield", window_start=500.0)
    assert m["fraud_value_sent"] == 12000 and m["fraud_value_through"] == 5000 and m["fraud_count_through"] == 1
    assert m["fail_open_value"] == 5000 and m["attack_sent"] == 2 and m["attack_shed"] == 1 and m["attack_reached_bank"] == 1
    assert m["legit_sent"] == 5 and m["legit_completed"] == 3 and m["legit_held"] == 1 and m["legit_blocked"] == 1
    assert m["deadline_miss_rate"] == pytest.approx(1 / 5) and m["p99_ms"] == 150.0
    assert m["time_to_detect_s"] == pytest.approx(2.0) and m["false_coordinated_s"] == 1
    assert m["reconcile_mismatch"] == 0   # every ALLOW (or verified VERIFY) was posted, nothing else was
    held_but_posted = metrics.compute(truth, postings + [{"txn_id": "t-000000003", "amount": 100.0}], bank_log, [], threat,
                                      ledger, True, {}, "shield", window_start=500.0)
    assert held_but_posted["reconcile_mismatch"] == 1 and held_but_posted["scores"]["integrity"] == 0
    assert m["shadow_uplift"] == {"fraud_count": 1, "fraud_value": 5000.0, "legit_would_hold_per_1000": 0.0}
    s = m["scores"]
    assert s["protection"] == pytest.approx(100 * (1 - 5000 / 12000), rel=1e-3) and s["customer"] == 60.0
    assert s["integrity"] == 100 and s["shield_score"] is not None
    flat = metrics.flatten(m)
    assert flat["legit_completed_rate"] == 0.6 and flat["attack_shed_share"] == 0.5 and flat["shadow_fraud_count"] == 1


@pytest.mark.parametrize("system", metrics.BANK_SYSTEMS)
def test_bank_systems_have_no_ledger_or_detection(system):
    truth = [truth_line(1, "bot", fp="x", ts=0.0)]
    threat = [{"ts": "0", "level": "COORDINATED", "clusters": json.dumps([{"key": "x", "action": "throttle"}])}]
    m = metrics.compute(truth, [], [], [], threat, [], None, {}, system, 0.0)
    assert m["ledger_ok"] is None and m["scores"]["integrity"] is None and m["time_to_detect_s"] is None
    assert metrics.compute(truth, [], [], [], threat, [], True, {}, "shield", 0.0)["time_to_detect_s"] == 1.0


def _write_run(d, scenario, system, run_id, through, valid=True, p99=20.0):
    res = {"scenario": scenario, "system": system, "run_id": run_id, "valid": valid, "command": "cmd", "commit": "abc",
           "host": "lxone", "arch": "s390x", "fraud_value_sent": 1000.0, "fraud_value_through": through, "fail_open_value": 0,
           "attack_sent": 10, "attack_shed": 9, "legit_sent": 1000, "legit_completed": 995, "legit_held": 0, "legit_blocked": 0,
           "legit_verify": 5, "p50_ms": 5, "p95_ms": 10, "p99_ms": p99, "deadline_miss_rate": 0.0, "ledger_ok": True,
           "reconcile_mismatch": 0, "scores": {"protection": 100 * (1 - through / 1000), "customer": 99.5, "timeliness": 100.0,
                                               "integrity": 100, "shield_score": 99.0}}
    with open(os.path.join(d, f"{scenario}__{system}__{run_id}.json"), "w") as fh:
        json.dump(res, fh)


def test_summary_medians_targets_and_markdown(tmp_path):
    for i, v in enumerate((900.0, 800.0, 850.0)):
        _write_run(tmp_path, "S4", "bank_only", f"b{i}", v)
    for i, v in enumerate((100.0, 140.0, 120.0)):
        _write_run(tmp_path, "S4", "shield", f"s{i}", v)
    _write_run(tmp_path, "S4", "shield", "bad", 0.0, valid=False)
    _write_run(tmp_path, "S1", "bank_only", "x1", 0.0, p99=10.0)
    _write_run(tmp_path, "S1", "shield", "x2", 0.0, p99=15.0)
    s = summary.build(str(tmp_path), model_report=str(tmp_path / "none.json"))
    cell = s["matrix"]["S4"]["shield"]
    assert cell["median"]["fraud_value_through"] == 120.0 and cell["min"]["fraud_value_through"] == 100.0
    assert cell["valid_runs"] == 3 and len(cell["runs"]) == 4 and cell["invalid"][0]["run_id"] == "bad"
    assert cell["targets"]["fraud_value_through"]["pass"] is True        # 120 <= 0.2 * 850
    assert s["matrix"]["S1"]["shield"]["median"]["shield_overhead_ms"] == 5.0
    assert s["matrix"]["S1"]["shield"]["overhead_reference"] == "bank_only"   # no realistic-bank run here
    assert s["matrix"]["S1"]["shield"]["targets"]["shield_overhead_ms"]["pass"] is True
    assert "targets" in s["matrix"]["S4"]["bank_only"] and s["matrix"]["S4"]["bank_only"]["targets"] == {}
    md = summary.markdown(s, quantum_summary=str(tmp_path / "none.json"))
    assert "## S4" in md and "(pass)" in md and "invalid `bad`" in md


def test_overhead_is_measured_against_the_realistic_bank(tmp_path):
    _write_run(tmp_path, "S1", "bank_only", "o1", 0.0, p99=10.0)
    _write_run(tmp_path, "S1", "bank_realistic", "r1", 0.0, p99=12.0)
    _write_run(tmp_path, "S1", "shield", "s1", 0.0, p99=15.0)
    s = summary.build(str(tmp_path), model_report=str(tmp_path / "none.json"))
    shield = s["matrix"]["S1"]["shield"]
    assert shield["median"]["shield_overhead_ms"] == 3.0 and shield["overhead_reference"] == "bank_realistic"
    assert "bank_realistic" in shield["targets"]["shield_overhead_ms"]["target"]
    assert s["matrix"]["S1"]["bank_realistic"]["targets"] == {}     # reported only, no target set in advance


def test_runner_plan_alternates_and_bank_rule():
    p = runner.plan(should=False)
    assert len(p) == 36 and p[0] == ("S1", "shield", 1) and p[12] == ("S1", "bank_only", 2)
    cells = [(scen, system) for scen, system, _ in p]
    assert all(cells.count(c) == 3 for c in cells) and ("S2", "bank_only") not in cells
    assert len(runner.plan(should=True)) == 36 + len(runner.SHOULD)
    assert runner.bank_service_ms(200, 8) == 10.0          # 8 slots * 1000 / 10 ms = 800/s = 4 * 200
    assert runner.fp_limit_rps(200) == 480.0               # 2.4R: twice the bank app's payday rate
    real = runner.bank_settings("bank_realistic", 200, 8, 10.0, 10000.0)
    assert real == {"slots": 8, "service_ms": 10.0, "fail_mode": "bounded", "fallback_limit": 10000.0, "fp_limit_rps": 480.0}
    assert runner.bank_settings("shield", 200, 8, 10.0, 10000.0) == dict(real, fail_mode="open", fp_limit_rps=None)
    assert runner.bank_settings("bank_realistic", 200, 8, 10.0, 10000.0, "closed")["fail_mode"] == "closed"
    cap = runner.capacity_formula({"edge": 30, "decision": 250, "sentinel": 5, "bank": 50}, pass_share=0.24)
    assert cap["cores_score_all"] == pytest.approx(33.5) and cap["cores_cascade"] < 0.4 * cap["cores_score_all"]


def test_offline_checks_replay_ledger_and_provenance(tmp_path, monkeypatch):
    import fakeredis

    from ledger.writer import LedgerWriter

    monkeypatch.setattr(checks, "DATA", str(tmp_path))
    (tmp_path / "replay").mkdir()
    with open(tmp_path / "replay" / "hi_small_test.csv", "w") as fh:
        fh.write("txn_id,ts,from_acct,to_acct,amount,currency,format,label\n")
        for i in range(10001):
            fh.write(f"t-{i:09d},{1000 + i},A,B,1.0,USD,ACH,{1 if i % 1000 == 0 else 0}\n")
    checks.c08()
    w = LedgerWriter(fakeredis.FakeRedis(decode_responses=True), str(tmp_path / "ledger" / "ledger.jsonl"))
    w.append([{"ts": 1.0, "kind": "decision", "action": "ALLOW", "txn_id": f"t-{i:09d}"} for i in range(120)])
    checks.c05()
    repo = tmp_path / "repo"
    (repo / "eval" / "results").mkdir(parents=True)
    (repo / "quantum" / "results").mkdir(parents=True)
    monkeypatch.setattr(checks, "REPO", str(repo))
    with pytest.raises(checks.Skip):
        checks.c16()
    (repo / "eval" / "results" / "S1__shield__x.json").write_text(json.dumps({"run_id": "a", "command": "c", "commit": "z"}))
    checks.c16()
    (repo / "eval" / "results" / "summary.json").write_text(json.dumps({"matrix": {"S1": {"shield": {"runs": ["a", "ghost"]}}}}))
    with pytest.raises(checks.Fail, match="ghost"):
        checks.c16()


def test_check_c15_static_part_passes_on_this_repo(monkeypatch):
    class R:
        def zrevrange(self, *a):
            return []

    monkeypatch.setattr(checks, "_redis", lambda: R())
    checks.c15()
