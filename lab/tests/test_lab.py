"""Lab tests against a fake edge (no sockets): truth lines, VERIFY, kinds, determinism, host constant, rate cap."""
import asyncio
import csv
import json

import httpx
import pytest

from lab import config
from lab.engine import Lab
from lab.identities import Population
from lab.scenario import load

HEADER = ["txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format", "label"]


def write_replay(path, n=3000, launder_every=50):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i in range(n):
            w.writerow([f"t-{i:09d}", 1660000000 + i, f"A{i % 300:05d}", f"B{i % 97:05d}", 100 + i % 50, "USD", "ACH",
                        1 if i % launder_every == 0 else 0])


class FakeEdge:
    def __init__(self):
        self.pay, self.verify, self.headers = [], [], []

    def handler(self, request):
        assert request.url.host == "127.0.0.1"
        body = json.loads(request.content)
        if request.url.path == "/pay":
            self.pay.append(body)
            self.headers.append(dict(request.headers))
            action = "VERIFY" if int(body["txn_id"][2:]) % 3 == 0 else "ALLOW"
            return httpx.Response(200, json={"contract": 2, "txn_id": body["txn_id"], "action": action})
        if request.url.path == "/verify":
            self.verify.append(body)
            return httpx.Response(200, json={"verified": True})
        return httpx.Response(404)


def scenario_file(tmp_path, pops, duration=2.0):
    spec = {"id": "T1", "duration_s": duration, "warmup_s": 0, "seed": 3, "populations": pops, "faults": []}
    p = tmp_path / "T1.json"
    p.write_text(json.dumps(spec))
    return str(p)


def run_lab(tmp_path, pops, base_rps=100, duration=2.0):
    replay = tmp_path / "replay.csv"
    write_replay(replay)
    sc = load(scenario_file(tmp_path, pops, duration), base_rps)
    edge = FakeEdge()
    truth = tmp_path / "truth.jsonl"
    lab = Lab(sc, "t1-shield-01", "shield", str(replay), str(truth), transport=httpx.MockTransport(edge.handler))
    summary = asyncio.run(lab.run())
    lines = [json.loads(l) for l in truth.read_text().splitlines()]
    return summary, lines, edge


def test_truth_line_per_request_with_response_fields(tmp_path):
    summary, lines, edge = run_lab(tmp_path, [{"kind": "replay", "rate_x": 1.0, "start_s": 0, "stop_s": 2}])
    assert summary["sent"] == len(lines) == len(edge.pay) and summary["sent"] > 150
    assert summary["valid"] is True
    for line in lines:
        assert set(line) == {"run_id", "system", "req_id", "txn_id", "ts_sent", "kind", "label", "amount", "fp", "ip",
                             "asn", "status", "action", "rtt_ms", "verified"}
        assert line["status"] == 200 and line["kind"] in ("legit", "launder")
        assert (line["kind"] == "launder") == (line["label"] == 1)
    verifies = [l for l in lines if l["action"] == "VERIFY"]
    assert verifies and all(l["verified"] in (True, False) for l in verifies)
    assert sum(l["verified"] is True for l in verifies) == len(edge.verify)
    h = edge.headers[0]
    assert {"x-request-id", "x-forwarded-for", "x-client-fp", "x-client-asn"} <= set(h)
    assert h["x-forwarded-for"].startswith(("198.18.", "198.19."))


def test_bots_and_card_testers_use_synthetic_ids_and_never_verify(tmp_path):
    pops = [{"kind": "bot", "rate_x": 1.0, "start_s": 0, "stop_s": 2, "identities": 200, "tool_fps": 1, "networks": 3},
            {"kind": "cardtest", "rate_x": 0.2, "start_s": 0, "stop_s": 2}]
    summary, lines, edge = run_lab(tmp_path, pops)
    assert {l["kind"] for l in lines} == {"bot", "cardtest"}
    assert all(int(l["txn_id"][2:]) >= config.SYNTHETIC_TXN_START for l in lines)
    assert all(l["verified"] in (False, None) for l in lines) and edge.verify == []
    bots = [b for b in edge.pay if b["from_acct"].startswith("lab-")]
    assert bots and all(b["amount"] <= 500 for b in bots)
    cards = [l for l in lines if l["kind"] == "cardtest"]
    assert cards and all(l["amount"] <= 5.0 and l["label"] == 1 for l in cards)


def test_attacker_trickle_uses_bot_identities(tmp_path):
    pops = [{"kind": "bot", "rate_x": 1.0, "start_s": 0, "stop_s": 2, "identities": 100, "tool_fps": 1, "networks": 3},
            {"kind": "launder", "rate_rps": 10, "start_s": 0, "stop_s": 2, "identity": "attacker"}]
    _, lines, _ = run_lab(tmp_path, pops)
    bot_fps = {l["fp"] for l in lines if l["kind"] == "bot"}
    launder = [l for l in lines if l["kind"] == "launder"]
    assert launder and {l["fp"] for l in launder} <= bot_fps


def test_same_seed_same_sequence(tmp_path):
    pops = [{"kind": "replay", "rate_x": 1.0, "start_s": 0, "stop_s": 1}]
    a = [p["txn_id"] for p in run_lab(tmp_path, pops, duration=1.0)[2].pay]
    (tmp_path / "truth.jsonl").unlink()
    b = [p["txn_id"] for p in run_lab(tmp_path, pops, duration=1.0)[2].pay]
    n = min(len(a), len(b))
    assert n > 50 and sorted(a[:n]) == sorted(b[:n])


def test_customer_identity_is_stable_and_fingerprints_look_alike():
    pop = Population(9)
    assert pop.customer("X1") == pop.customer("X1")
    bot = pop.bots(10, 1, 3)[0]
    assert bot.fp.startswith("fp-") and len(bot.fp) == len(pop.app_fingerprint)


def test_host_is_a_constant_and_cap_refuses(tmp_path):
    assert config.TARGET_HOST == "127.0.0.1" and config.base_url().startswith("http://127.0.0.1:")
    replay = tmp_path / "replay.csv"
    write_replay(replay)
    sc = load(scenario_file(tmp_path, [{"kind": "replay", "rate_x": 50.0, "start_s": 0, "stop_s": 1}]), 100)
    with pytest.raises(ValueError, match="cap"):
        asyncio.run(Lab(sc, "x", "shield", str(replay), str(tmp_path / "t.jsonl")).run())


def test_entry_only_picks_the_port_on_the_fixed_host(monkeypatch):
    monkeypatch.setenv("RF_PORT_OFFSET", "5")
    assert config.base_url() == "http://127.0.0.1:8085" and config.base_url("perimeter") == "http://127.0.0.1:8005"
    with pytest.raises(ValueError, match="entry"):
        Lab(load("S1", 10), "x", "shield", "replay.csv", "truth.jsonl", entry="internet")


def test_shipped_scenarios_load_and_fit_the_cap():
    for sid in ("S1", "S2", "S3", "S4", "S4b", "S5", "S6"):
        sc = load(sid, 200)
        assert sc.duration_s == 210 and sc.peak_rps() <= config.MAX_TOTAL_RPS
    assert load("S4", 200).pops[-1].options["identity"] == "attacker"
    assert load("S4b", 200).pops[-1].options["identity"] == "clean"
    s6 = load("S6", 200)
    assert s6.pops[1].options["use_app_fp"] is True and s6.pops[-1].options["identity"] == "attacker"


def test_borrowed_fingerprint_bots_share_the_bank_app_fingerprint(tmp_path):
    replay = tmp_path / "replay.csv"
    write_replay(replay)
    lab = Lab(load("S6", 200), "s6-shield-01", "shield", str(replay), str(tmp_path / "t.jsonl"))
    assert len(lab.bots) == 2000 and all(b.fp == lab.pop.app_fingerprint for b in lab.bots)
    assert len({b.asn for b in lab.bots}) <= 3 and all(b.acct.startswith("lab-") for b in lab.bots)
