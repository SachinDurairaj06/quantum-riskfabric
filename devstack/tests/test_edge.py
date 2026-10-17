"""Edge stand-in tests: headers, limits, cluster actions, routing, bypass and edge.log fields (prd/06 D-06 to D-25)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from devstack.edge import IP_LIMIT_RPS, THROTTLE_RPS, ClusterMap, Edge, create_app

PAY = {"txn_id": "t-000000001", "ts": 1.0, "from_acct": "card-1-0", "to_acct": "merchant-9", "amount": 12.5,
       "currency": "USD", "format": "chip"}
EDGE_LOG = set("ts req_id ip fp asn method path status ms backend cluster edge_action known".split())


class Upstream:
    def __init__(self, down=()):
        self.down, self.calls = set(down), []

    def __call__(self, request: httpx.Request):
        hostport = f"{request.url.host}:{request.url.port}"
        if hostport in self.down:
            raise httpx.ConnectError("refused")
        self.calls.append((hostport, request.url.path, dict(request.headers)))
        return httpx.Response(200, json={"action": "ALLOW"})


def client_for(mode, upstream, **edge_state):
    app = create_app(mode, transport=httpx.MockTransport(upstream), background=False)
    for k, v in edge_state.items():
        setattr(app.state.edge, k, v)
    return app


def headers(ip="198.18.0.1", fp="fp-aaaa0001", asn="64512", **extra):
    return dict({"X-Forwarded-For": ip, "X-Client-FP": fp, "X-Client-ASN": asn, "X-Request-Id": "r-1"}, **extra)


def test_statusquo_routes_to_the_bank_channel_with_the_contract_headers():
    up = Upstream()
    app = client_for("statusquo", up)
    with TestClient(app) as c:
        spoofed = {"X-Client-Known": "1", "X-Cluster-Risk": "0.99", "X-Edge-Action": "stepup"}
        assert c.post("/pay", json=PAY, headers=headers(**spoofed)).status_code == 200
        assert c.post("/verify", json={"txn_id": "t-000000001", "token": "lab-ok"}, headers=headers()).status_code == 200
        assert c.post("/other", json={}, headers=headers()).status_code == 404
    (host, path, h), (_, vpath, _) = up.calls
    assert (host, path, vpath) == ("127.0.0.1:9000", "/bank/pay", "/bank/verify")
    assert h["x-client-ip"] == "198.18.0.1" and h["x-request-id"] == "r-1" and float(h["x-edge-ts"]) > 0
    assert h["x-deadline-ms"] == "100" and "x-cluster" not in h
    assert "x-cluster-risk" not in h and "x-edge-action" not in h                  # a client can't set them
    entries = app.state.edge.log_buffer
    assert set(entries[0]) == EDGE_LOG and entries[0]["known"] == "1" and entries[0]["backend"] == "9000"
    assert entries[2]["status"] == "404"


def test_per_ip_limit_in_every_mode():
    for mode in ("statusquo", "realistic", "riskfabric"):
        edge = Edge(mode)
        out = [edge.gate("198.18.0.9", "fp-x", "1", 100.5)[0] for _ in range(int(IP_LIMIT_RPS) + 1)]
        assert out[:-1] == [None] * int(IP_LIMIT_RPS) and out[-1] == 429


def test_realistic_limits_each_fingerprint_over_the_last_second():
    edge = Edge("realistic")
    edge.fp_limit = 3.0
    statuses = [edge.gate(f"198.18.0.{i}", "fp-app", "1", 10.0 + i * 0.1)[0] for i in range(5)]
    assert statuses == [None, None, None, 429, 429]
    assert edge.gate("198.18.1.1", "fp-other", "1", 10.5)[0] is None          # other fingerprints pass
    assert edge.gate("198.18.1.2", "fp-app", "1", 12.0)[0] is None            # the second has passed


def test_cluster_actions_from_the_latest_threat_entry():
    m = ClusterMap()
    m.load([{"id": "c-1", "key_type": "fp_asn", "key": "fp-app|64900", "action": "stepup", "until": 0},
            {"id": "c-2", "key_type": "fp", "key": "fp-tool", "action": "block", "until": 0},
            {"id": "c-3", "key_type": "fp", "key": "fp-slow", "action": "throttle", "until": 0},
            {"id": "c-4", "key_type": "fp", "key": "fp-old", "action": "block", "until": 5.0}])
    edge = Edge("riskfabric")
    edge.clusters = m
    assert edge.gate("198.18.0.1", "fp-app", "64900", 100.0) == (None, "c-1", "stepup")
    assert edge.gate("198.18.0.2", "fp-app", "64512", 100.0) == (None, "", "pass")     # same fp, other network
    assert edge.gate("198.18.0.3", "fp-tool", "1", 100.0) == (403, "c-2", "block")
    assert edge.gate("198.18.0.4", "fp-old", "1", 100.0) == (None, "", "pass")          # expired
    throttled = [edge.gate(f"198.18.2.{i}", "fp-slow", "1", 200.0)[0] for i in range(int(THROTTLE_RPS) + 1)]
    assert throttled[:-1] == [None] * int(THROTTLE_RPS) and throttled[-1] == 429
    # step-up is never looser than throttle: the same cap, plus the header on what passes
    stepped = [edge.gate(f"198.18.3.{i}", "fp-app", "64900", 300.0) for i in range(int(THROTTLE_RPS) + 1)]
    assert stepped[0] == (None, "c-1", "stepup") and stepped[-1] == (429, "c-1", "stepup")


def test_riskfabric_adds_signals_fails_over_and_bypasses():
    up = Upstream()
    app = client_for("riskfabric", up)
    m = ClusterMap()
    m.load([{"id": "c-7", "key_type": "fp", "key": "fp-bot", "action": "stepup", "until": 0}])
    app.state.edge.clusters = m
    with TestClient(app) as c:
        c.post("/pay", json=PAY, headers=headers(fp="fp-bot"))
        host, path, h = up.calls[-1]
        assert (host, path, h["x-cluster"], h["x-edge-action"]) == ("127.0.0.1:8001", "/pay", "c-7", "stepup")
        up.down = {"127.0.0.1:8001"}
        c.post("/pay", json=PAY, headers=headers(ip="198.18.0.2"))
        assert up.calls[-1][:2] == ("127.0.0.1:8002", "/pay")                  # the standby
        up.down = {"127.0.0.1:8001", "127.0.0.1:8002"}
        c.post("/pay", json=PAY, headers=headers(ip="198.18.0.3"))
        assert up.calls[-1][:2] == ("127.0.0.1:9000", "/bank/pay")              # bypass to the bank's own path
        assert app.state.edge.log_buffer[-1]["backend"] == "9000"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        Edge("cloudflare")
