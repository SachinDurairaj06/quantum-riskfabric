"""Perimeter tests: each layer's checks, forwarding to the edge, the stats, and the ceiling from measured capacity."""
import json

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from perimeter import limit
from perimeter.app import create_app, read_policy
from perimeter.layers import Perimeter, Policy

PAY = {"txn_id": "t-000000001", "ts": 1.0, "from_acct": "card-1-0", "to_acct": "merchant-9", "amount": 12.5,
       "currency": "USD", "format": "chip"}
JSON = {"content-type": "application/json", "x-forwarded-for": "198.18.0.1"}


def check(p, body=PAY, path="/pay", method="POST", headers=JSON, now=1000.0):
    return p.check(method, path, headers, json.dumps(body).encode(), now)


def test_a_clean_payment_passes_every_layer():
    p = Perimeter()
    assert check(p) is None and p.passed == 1


def test_cdn_layer_blocklist_and_per_ip_rate():
    p = Perimeter(Policy(ip_rps=3, blocklist=frozenset({"198.18.9.9"})))
    assert check(p, headers=dict(JSON, **{"x-forwarded-for": "198.18.9.9"}))[:2] == (403, "cdn")
    assert [check(p, now=50.2) for _ in range(3)] == [None, None, None]
    assert check(p, now=50.7) == (429, "cdn", "per-IP rate limit")
    assert check(p, now=51.0) is None                                        # a new second
    assert check(p, headers=dict(JSON, **{"x-forwarded-for": "198.18.0.2"}), now=50.8) is None


@pytest.mark.parametrize("kwargs,reason", [
    ({"path": "/admin"}, "route not allowed"),
    ({"method": "GET"}, "route not allowed"),
    ({"headers": {"x-forwarded-for": "198.18.0.1", "content-type": "text/plain"}}, "not JSON"),
    ({"body": dict(PAY, format="x" * 9000)}, "body too large"),
    ({"body": dict(PAY, from_acct="1' OR '1'='1")}, "sql injection"),
    ({"body": dict(PAY, to_acct="x; DROP TABLE accounts")}, "sql injection"),
    ({"body": dict(PAY, format="<script>alert(1)</script>")}, "script injection"),
    ({"body": dict(PAY, to_acct="../../etc/passwd")}, "path traversal"),
])
def test_waf_rules(kwargs, reason):
    assert check(Perimeter(), **kwargs) == (403, "waf", reason)


def test_merchant_ids_with_minus_signs_are_not_sql():
    assert check(Perimeter(), body=dict(PAY, to_acct="merchant--727612092139916043")) is None


def test_gateway_ceiling_and_schema():
    p = Perimeter()
    p.set_policy(Policy(ip_rps=1000, ceiling_rps=2), now=10.0)
    results = [check(p, now=10.0) for _ in range(3)]
    assert results[:2] == [None, None] and results[2] == (429, "gateway", "capacity ceiling")
    assert check(p, now=11.0) is None                                        # the bucket refills
    bad = Perimeter()
    assert check(bad, body=dict(PAY, amount=-1))[:2] == (400, "gateway")
    assert check(bad, body=dict(PAY, txn_id="x"))[:2] == (400, "gateway")
    assert check(bad, body=dict(PAY, currency="usd"))[:2] == (400, "gateway")
    assert check(bad, body={"txn_id": "t-000000001", "token": "lab-ok"}, path="/verify") is None


def test_app_forwards_unchanged_and_counts(tmp_path):
    seen = []

    def upstream(request: httpx.Request):
        seen.append((request.url.path, json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={"action": "ALLOW"}, headers={"retry-after": "1"})

    app = create_app(upstream="http://edge", transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        ok = c.post("/pay", json=PAY, headers={"X-Forwarded-For": "198.18.0.1", "X-Request-Id": "r1"})
        blocked = c.post("/pay", json=dict(PAY, format="<script>"), headers={"X-Forwarded-For": "198.18.0.1"})
        stats = c.get("/perimeter/stats").json()
    assert ok.status_code == 200 and ok.json() == {"action": "ALLOW"} and ok.headers["retry-after"] == "1"
    assert seen[0][0] == "/pay" and seen[0][1] == PAY and seen[0][2]["x-request-id"] == "r1"
    assert blocked.status_code == 403 and blocked.json()["layer"] == "waf" and len(seen) == 1
    assert stats["received"] == 2 and stats["passed"] == 1 and stats["stopped"]["waf"] == {"script injection": 1}


def test_an_oversized_body_is_refused_without_reaching_the_edge():
    seen = []
    app = create_app(upstream="http://edge", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)))
    with TestClient(app) as c:
        r = c.post("/pay", content=b"{" + b" " * 20000 + b"}", headers={"Content-Type": "application/json",
                                                                        "X-Forwarded-For": "198.18.0.1"})
        stats = c.get("/perimeter/stats").json()
    assert r.status_code == 403 and r.json() == {"error": "body too large", "layer": "waf"} and seen == []
    assert stats["received"] == 1 and stats["stopped"]["waf"] == {"body too large": 1}


def test_edge_down_is_a_502():
    def upstream(request):
        raise httpx.ConnectError("refused")

    with TestClient(create_app(upstream="http://edge", transport=httpx.MockTransport(upstream))) as c:
        r = c.post("/pay", json=PAY, headers={"X-Forwarded-For": "198.18.0.1"})
    assert r.status_code == 502


def test_policy_comes_from_valkey():
    import asyncio

    r = fakeredis.FakeAsyncRedis(decode_responses=True)

    async def go():
        assert (await read_policy(r)) == Policy()
        await r.set("cfg:perimeter_ip_rps", "5")
        await r.set("cfg:perimeter_limit_rps", "1200")
        await r.sadd("perimeter:blocklist", "198.18.1.1")
        return await read_policy(r)

    assert asyncio.run(go()) == Policy(ip_rps=5.0, ceiling_rps=1200.0, blocklist=frozenset({"198.18.1.1"}))


def test_ceiling_comes_only_from_a_measured_capacity(tmp_path):
    cap = {"cores": 4, "cpu_us_per_req": {"edge": 30.0, "decision": 250.0, "sentinel": 5.0, "bank": 55.0},
           "host": "lxone", "command": "python -m eval.scenarios.runner capacity --base-rps 200"}
    out = limit.ceiling(cap)
    assert out["per_request_us"] == 340.0 and out["ceiling_rps"] == int(0.8 * 4 * 1e6 / 340.0)
    with pytest.raises(ValueError, match="cores"):
        limit.ceiling(dict(cap, cores=None))
    with pytest.raises(ValueError, match="bank"):
        limit.ceiling(dict(cap, cpu_us_per_req={"edge": 1, "decision": 1, "sentinel": 1}))
    assert limit.latest_capacity(str(tmp_path)) is None
    assert limit.main(["--results", str(tmp_path)]) == 1                     # no measurement, no ceiling
    (tmp_path / "CAP__shield__202610171200.json").write_text(json.dumps({"valid": True, "host": "another-host",
                                                                         "capacity": dict(cap, host="another-host")}))
    assert limit.main(["--results", str(tmp_path)]) == 1                     # never another machine's numbers
