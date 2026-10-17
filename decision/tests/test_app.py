"""Decision service tests: contract shape, stand-in, Shield router, settle, verify, standby (prd/07)."""
import asyncio
import json
import time
import types

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from decision.app import create_app
from decision.config import Config

BODY = {"txn_id": "t-000123456", "ts": 1792310400.123, "from_acct": "8000EBD30", "to_acct": "8000F5340",
        "amount": 1250.0, "currency": "USD", "format": "ACH"}
RESPONSE_FIELDS = {"contract", "txn_id", "action", "path", "stand_in", "score", "model", "reasons", "tier",
                   "cluster", "load", "latency_ms", "deadline_ms", "deadline_met", "mode", "engine"}


class Unavailable(Exception):
    pass


def fake_scoring(score=0.1, delay_s=0.0, raise_unavailable=False, features=None):
    mod = types.ModuleType("fake_scoring")
    mod.Unavailable = Unavailable
    mod.ingested = []
    mod.load = lambda model_dir: None

    def score_fn(payment, signals, timeout_ms=40):
        if raise_unavailable:
            raise Unavailable("down")
        if delay_s:
            time.sleep(delay_s)
        return {"score": score, "reasons": ["fan-out burst"], "model": "gfp-gbm-v1", "feature_hash": "abc",
                "latency_ms": 1.0, "features": features or {"f_amount": 1.0}}

    mod.score = score_fn
    mod.ingest = lambda payment: mod.ingested.append(payment["txn_id"])
    return mod


class FakeBank:
    """Records calls; engine behaviour is configurable."""

    def __init__(self, decision="ALLOW", status=200, delay_s=0.0):
        self.decision, self.status, self.delay_s = decision, status, delay_s
        self.engine_calls, self.settles = [], []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path == "/engine/pay":
            self.engine_calls.append({"headers": dict(request.headers), "body": body})
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "fail"})
            return httpx.Response(200, json={"decision": self.decision, "score": 0.3, "reasons": ["R4"],
                                             "engine_latency_ms": 1.0, "queue_ms": 0.0})
        if request.url.path == "/bank/settle":
            self.settles.append(body)
            return httpx.Response(200, json={"txn_id": body["payment"]["txn_id"], "posted": body["action"] == "ALLOW",
                                             "duplicate": False})
        return httpx.Response(404)


def make_cfg(**over):
    base = dict(valkey_url="redis://unused", deadline_ms=100, mode="standalone", variant="riskfabric", replica="8001",
                engine_url="http://bank.test", data_dir="/tmp/rf", port_offset=0)
    base.update(over)
    return Config(**base)


def client_for(cfg, scoring_mod, bank, redis):
    app = create_app(cfg=cfg, redis=redis, bank_transport=httpx.MockTransport(bank.handler), scoring_module=scoring_mod)
    return TestClient(app)


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def redis():
    server = fakeredis.FakeServer()
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True), fakeredis.FakeRedis(server=server, decode_responses=True)


def test_standalone_allow_has_contract_fields_streams_and_settle(redis):
    aredis, sync = redis
    bank = FakeBank()
    with client_for(make_cfg(), fake_scoring(0.1), bank, aredis) as c:
        r = c.post("/pay", json=BODY, headers={"X-Request-Id": "req-1", "X-Edge-Ts": str(time.time())})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == RESPONSE_FIELDS
        assert body["contract"] == 2 and body["action"] == "ALLOW" and body["path"] == "full"
        assert body["stand_in"] is False and body["tier"] is None and body["engine"] is None
        assert body["deadline_met"] is True and body["mode"] == "standalone"
        assert wait_for(lambda: len(bank.settles) == 1)
        assert bank.settles[0]["action"] == "ALLOW" and bank.settles[0]["via"] == "standalone"
        assert wait_for(lambda: sync.xlen("payments") == 1)
    dec = sync.xrange("decisions")[0][1]
    assert dec["req_id"] == "req-1" and dec["stand_in"] == "0" and dec["deadline_met"] == "1"
    rec = json.loads(sync.xrange("ledger.in")[0][1]["rec"])
    assert rec["kind"] == "decision" and rec["policy"] and rec["txn_id"] == BODY["txn_id"]


def test_hold_writes_queue_and_features(redis):
    aredis, sync = redis
    with client_for(make_cfg(), fake_scoring(0.95, features={"f_x": 2.0}), FakeBank(), aredis) as c:
        assert c.post("/pay", json=BODY).json()["action"] == "HOLD"
    assert sync.zscore("hold:queue", BODY["txn_id"]) == pytest.approx(0.95)
    held = sync.hgetall(f"hold:{BODY['txn_id']}")
    assert json.loads(held["features"]) == {"f_x": 2.0} and float(held["score"]) == pytest.approx(0.95)
    assert 0 < sync.ttl(f"hold:{BODY['txn_id']}") <= 24 * 3600


def test_stepup_turns_allow_into_verify_and_verify_settles(redis):
    aredis, _ = redis
    bank = FakeBank()
    with client_for(make_cfg(), fake_scoring(0.1), bank, aredis) as c:
        assert c.post("/pay", json=BODY, headers={"X-Edge-Action": "stepup"}).json()["action"] == "VERIFY"
        assert c.post("/verify", json={"txn_id": BODY["txn_id"], "token": "wrong"}).status_code == 403
        ok = c.post("/verify", json={"txn_id": BODY["txn_id"], "token": "lab-ok"})
        assert ok.status_code == 200 and ok.json() == {"txn_id": BODY["txn_id"], "action": "ALLOW", "verified": True}
        assert c.post("/verify", json={"txn_id": BODY["txn_id"], "token": "lab-ok"}).status_code == 403
        assert wait_for(lambda: [s["action"] for s in bank.settles] == ["VERIFY", "ALLOW"])


@pytest.mark.parametrize("tier,amount,expected", [
    ("low", 50.0, "ALLOW"), ("low", 50000.0, "VERIFY"), ("medium", 50.0, "VERIFY"),
    ("high", 50.0, "HOLD"), (None, 50.0, "HOLD"),
])
def test_force_timeout_drill_uses_tiers(redis, tier, amount, expected):
    aredis, sync = redis
    sync.set("cfg:force_timeout", "1")
    if tier:
        sync.set(f"tier:{BODY['from_acct']}", tier)
    with client_for(make_cfg(), fake_scoring(0.1), FakeBank(), aredis) as c:
        body = c.post("/pay", json=dict(BODY, amount=amount)).json()
    assert body["action"] == expected and body["stand_in"] is True and body["score"] is None
    assert body["path"] == "stand_in" and body["tier"] == (tier or "unknown")
    assert body["reasons"] == ["stand-in: scoring unavailable", f"tier {tier or 'unknown'}"]


def test_scoring_unavailable_and_slow_scoring_fall_back_within_deadline(redis):
    aredis, sync = redis
    sync.set(f"tier:{BODY['from_acct']}", "low")
    with client_for(make_cfg(), fake_scoring(raise_unavailable=True), FakeBank(), aredis) as c:
        assert c.post("/pay", json=BODY).json()["path"] == "stand_in"
    with client_for(make_cfg(deadline_ms=60), fake_scoring(delay_s=0.5), FakeBank(), aredis) as c:
        start = time.time()
        body = c.post("/pay", json=BODY, headers={"X-Deadline-Ms": "60"}).json()
        assert body["path"] == "stand_in" and (time.time() - start) < 0.3


def test_shield_returns_engine_decision_unchanged_with_cluster_risk(redis):
    aredis, sync = redis
    sync.xadd("threat", {"ts": str(time.time()), "seq": "1", "level": "COORDINATED",
                         "clusters": json.dumps([{"id": "c-17", "risk": 0.86}])})
    bank = FakeBank(decision="VERIFY")
    with client_for(make_cfg(mode="shield"), fake_scoring(0.2), bank, aredis) as c:
        time.sleep(0.7)  # let the threat cache refresh
        body = c.post("/pay", json=BODY, headers={"X-Cluster": "c-17"}).json()
        assert body["action"] == "VERIFY" and body["path"] == "engine" and body["mode"] == "shield"
        assert body["engine"]["decision"] == "VERIFY" and body["engine"]["timeout"] is False
        assert body["load"] == "COORDINATED"
        assert bank.engine_calls[0]["headers"]["x-cluster-risk"] == "0.860"
        assert bank.engine_calls[0]["headers"]["x-cluster"] == "c-17"
        assert wait_for(lambda: any(json.loads(e[1]["rec"])["kind"] == "shadow" for e in sync.xrange("ledger.in")))
        assert wait_for(lambda: len(bank.settles) == 1 and bank.settles[0]["via"] == "shield")


@pytest.mark.parametrize("bank", [FakeBank(status=503), FakeBank(delay_s=0.5)])
def test_shield_engine_failure_or_delay_gives_stand_in(redis, bank):
    aredis, sync = redis
    sync.set(f"tier:{BODY['from_acct']}", "medium")
    with client_for(make_cfg(mode="shield"), fake_scoring(0.2), bank, aredis) as c:
        start = time.time()
        body = c.post("/pay", json=BODY).json()
        assert time.time() - start < 0.3
    assert body["path"] == "stand_in" and body["action"] == "VERIFY" and body["engine"]["timeout"] is True
    assert body["reasons"][0] == "stand-in: engine unavailable"


def test_shield_hold_gets_shadow_score_and_features(redis):
    aredis, sync = redis
    with client_for(make_cfg(mode="shield"), fake_scoring(0.91, features={"f_a": 3.0}), FakeBank(decision="HOLD"), aredis) as c:
        assert c.post("/pay", json=BODY).json()["action"] == "HOLD"
        assert wait_for(lambda: "features" in sync.hgetall(f"hold:{BODY['txn_id']}"))
    held = sync.hgetall(f"hold:{BODY['txn_id']}")
    assert float(held["score"]) == pytest.approx(0.91) and json.loads(held["features"]) == {"f_a": 3.0}


def test_invalid_body_is_400(redis):
    aredis, _ = redis
    with client_for(make_cfg(), fake_scoring(), FakeBank(), aredis) as c:
        assert c.post("/pay", json=dict(BODY, txn_id="bad")).status_code == 400
        assert c.post("/pay", content=b"not json", headers={"content-type": "application/json"}).status_code == 400


def test_health_and_stats(redis):
    aredis, _ = redis
    with client_for(make_cfg(), fake_scoring(), FakeBank(), aredis) as c:
        h = c.get("/health")
        assert h.status_code == 200
        assert h.json() == {"ok": True, "mode": "standalone", "role": "primary", "graph": "live", "model_loaded": True,
                            "valkey": True, "version": "dev"}
        c.post("/pay", json=BODY)
        s = c.get("/stats").json()
        assert s["counts"]["allow"] == 1 and s["replica"] == "8001" and "p99" in s["latency_ms"]


def test_health_503_when_model_fails_to_load_in_standalone(redis):
    aredis, _ = redis
    mod = fake_scoring()

    def broken(model_dir):
        raise RuntimeError("no model")

    mod.load = broken
    with client_for(make_cfg(), mod, FakeBank(), aredis) as c:
        h = c.get("/health")
        assert h.status_code == 503 and h.json()["model_loaded"] is False
        assert c.post("/pay", json=BODY).json()["path"] == "stand_in"


def test_downgrade_variant_refuses_to_start(redis):
    aredis, _ = redis
    with pytest.raises(RuntimeError, match="A-13"):
        with client_for(make_cfg(variant="submitted_design"), fake_scoring(), FakeBank(), aredis):
            pass


def test_standby_follows_other_replica_payments(redis):
    aredis, sync = redis
    mod = fake_scoring()
    with client_for(make_cfg(replica="8002"), mod, FakeBank(), aredis) as c:
        assert c.get("/health").json()["graph"] == "following"
        time.sleep(0.2)
        sync.xadd("payments", {"ts": "1.0", "txn_id": "t-000000001", "from_acct": "a", "to_acct": "b",
                               "amount": "5", "currency": "USD", "format": "ACH", "replica": "8001"})
        sync.xadd("payments", {"ts": "2.0", "txn_id": "t-000000002", "from_acct": "a", "to_acct": "b",
                               "amount": "5", "currency": "USD", "format": "ACH", "replica": "8002"})
        assert wait_for(lambda: mod.ingested == ["t-000000001"], timeout=3.0)
