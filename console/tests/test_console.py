"""Console API tests: panels read the contract keys, the scoreboard falls back to its empty state."""
import json
import time

import fakeredis
import httpx
from fastapi.testclient import TestClient

from console.app import QUANTUM_LABEL, create_app
from console.notes import case_note
from ledger.writer import LedgerWriter


def bank_handler(request):
    return httpx.Response(200, json={"ts": 0, "accounts": 10, "posted_count": 3, "posted_value": 300.0,
                                     "pending_count": 1, "fail_open_count": 0, "fail_open_value": 0})


def make(tmp_path, repo=None):
    server = fakeredis.FakeServer()
    aredis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    app = create_app(redis=aredis, data_dir=str(tmp_path), engine_url="http://bank.test",
                     repo_dir=str(repo or tmp_path), bank_transport=httpx.MockTransport(bank_handler))
    return TestClient(app), sync


def test_perimeter_panel_and_system_badge(tmp_path):
    stats = {"received": 10, "passed": 9, "stopped": {"cdn": {}, "waf": {"sql injection": 1}, "gateway": {}}}

    def build(handler):
        server = fakeredis.FakeServer()
        app = create_app(redis=fakeredis.FakeAsyncRedis(server=server, decode_responses=True), data_dir=str(tmp_path),
                         engine_url="http://bank.test", repo_dir=str(tmp_path),
                         bank_transport=httpx.MockTransport(bank_handler), perimeter_url="http://perimeter.test",
                         perimeter_transport=httpx.MockTransport(handler))
        return TestClient(app), fakeredis.FakeRedis(server=server, decode_responses=True)

    client, sync = build(lambda req: httpx.Response(200, json=stats))
    with client as c:
        assert c.get("/api/meta").json() == {"system": None}
        sync.set("cfg:system", "shield")
        assert c.get("/api/meta").json() == {"system": "shield"}
        p = c.get("/api/perimeter").json()
        assert p["reachable"] is True and p["passed"] == 9 and p["stopped"]["waf"] == {"sql injection": 1}

    def down(req):
        raise httpx.ConnectError("refused")

    client, _ = build(down)
    with client as c:
        assert c.get("/api/perimeter").json() == {"reachable": False}


def test_pages_and_empty_scoreboard(tmp_path):
    client, _ = make(tmp_path)
    with client as c:
        assert "Console" in c.get("/").text
        assert "Evaluation matrix" in c.get("/scoreboard").text
        r = c.get("/api/results/summary")
        assert r.status_code == 404 and r.json()["empty"] is True
        assert c.get("/api/quantum/summary").status_code == 404


def test_panels_read_streams_and_holds(tmp_path):
    client, r = make(tmp_path)
    now = time.time()
    r.xadd("threat", {"ts": str(now), "seq": "1", "level": "COORDINATED",
                      "clusters": json.dumps([{"id": "c-1", "key_type": "fp", "action": "block", "share": 0.4, "risk": 0.9}])})
    for status in ("200", "200", "429", "403"):
        r.xadd("edge.log", {"ts": str(now), "status": status})
    r.xadd("decisions", {"ts": str(now), "action": "HOLD", "path": "full", "stand_in": "0", "deadline_met": "1", "latency_ms": "7.5"})
    r.set("cfg:hold_cap_pct", "2")
    r.zadd("hold:queue", {"t-000000001": 0.9})
    r.hset("hold:t-000000001", mapping={"amount": "1250", "currency": "USD", "reasons": json.dumps(["fan-out burst"]),
                                        "stand_in": "0", "tier": "", "cluster": "c-1", "score": "0.9"})
    r.hset("qml:t-000000001", mapping={"score": "0.7", "status": "ok", "backend": "statevector", "model": "qp1-v1"})
    r.xadd("bank.log", {"entry": "engine", "latency_ms": "4.0", "timeout": "0", "posted": "0"})
    with client as c:
        t = c.get("/api/threat").json()["entries"][-1]
        assert t["level"] == "COORDINATED" and t["clusters"][0]["action"] == "block"
        assert c.get("/api/edge").json()["totals"] == {"passed": 2, "throttled": 1, "blocked": 1}
        d = c.get("/api/decisions").json()
        assert d["counts"]["HOLD"] == 1 and d["hold_alert"] is True and d["latency_ms"]["p99"] == 7.5
        h = c.get("/api/holds").json()
        assert h["challenger_label"] == QUANTUM_LABEL
        assert h["holds"][0]["challenger"]["score"] == 0.7 and "fan-out burst" in h["holds"][0]["note"]
        b = c.get("/api/bank").json()
        assert b["books"]["posted_count"] == 3 and b["engine_p99_ms"] == 4.0


def test_ledger_status_and_verify(tmp_path):
    client, r = make(tmp_path)
    w = LedgerWriter(r, str(tmp_path / "ledger" / "ledger.jsonl"))
    w.append([{"ts": 1.0, "kind": "decision", "txn_id": "t-000000001", "action": "ALLOW", "path": "engine"},
              {"ts": 1.1, "kind": "shadow", "txn_id": "t-000000001", "shadow_score": 0.95}])
    with client as c:
        assert c.get("/api/ledger").json()["records"] == 2
        v = c.post("/api/ledger/verify").json()
        assert v["ok"] is True and v["records"] == 2
        s = c.get("/api/shield").json()
        assert s["pairs"] == 1 and s["shadow_would_hold"] == 1


def test_summary_served_when_present(tmp_path):
    (tmp_path / "eval" / "results").mkdir(parents=True)
    (tmp_path / "eval" / "results" / "summary.json").write_text(json.dumps({"matrix": {"S4": {}}}))
    client, _ = make(tmp_path)
    with client as c:
        assert c.get("/api/results/summary").json() == {"matrix": {"S4": {}}}


def test_case_note_wording():
    note = case_note({"reasons": json.dumps(["fan-out burst", "cluster c-17"]), "tier": "high", "cluster": "c-17",
                      "amount": "1250", "currency": "USD", "stand_in": "0"})
    assert note == "Held: fan-out burst; account tier high; part of cluster c-17; amount 1,250.00 USD."
    assert case_note({"stand_in": "1", "tier": "unknown"}).startswith("Held by stand-in rules")
