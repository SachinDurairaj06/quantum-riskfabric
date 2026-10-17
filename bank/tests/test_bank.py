"""Bank tests (prd/03 Part 2): rules R0 to R5, the fail modes with the bounded fallback (BK-11), verify, settle,
the capacity queue, the live brownout switch, the test switches, bank.log and the contract shapes."""
import asyncio
import json
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from bank.app import create_app
from bank.banklog import BankLog
from bank.capacity import Capacity
from bank.engine import Engine
from bank.load import load
from bank.registry import Registry
from bank.rules import Rules, load_spec
from bank.settings import Settings

BODY = {"txn_id": "t-000123456", "ts": 1792310400.0, "from_acct": "card-1-0", "to_acct": "merchant-9",
        "amount": 120.0, "currency": "USD", "format": "chip"}
RESPONSE_TYPES = {"contract": int, "txn_id": str, "action": str, "path": str, "stand_in": bool, "reasons": list,
                  "latency_ms": (int, float), "deadline_ms": int, "deadline_met": bool, "mode": str}
BANK_LOG_FIELDS = set("ts txn_id req_id entry action latency_ms queue_ms timeout rules posted".split())


def write_registry(tmp_path):
    (tmp_path / "bank").mkdir(exist_ok=True)
    (tmp_path / "bank" / "accounts.csv").write_text(
        "acct,bank_id,segment,opening_balance,first_seen_ts\n"
        "card-1-0,card,retail,5000.0,1600000000\ncard-2-0,card,business,90000.0,1600000000\n", encoding="utf-8")
    (tmp_path / "bank" / "tiers.csv").write_text("acct,tier\ncard-1-0,low\ncard-2-0,high\n", encoding="utf-8")


@pytest.fixture
def stack(tmp_path):
    write_registry(tmp_path)
    server = fakeredis.FakeServer()
    app = create_app(redis=fakeredis.FakeAsyncRedis(server=server, decode_responses=True), data_dir=str(tmp_path))
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    with TestClient(app) as client:
        yield client, app.state.bank, sync, tmp_path
        time.sleep(0.3)   # let engine work abandoned at a deadline finish before the loop closes


def pay(client, **changes):
    return client.post("/bank/pay", json=dict(BODY, **changes), headers={"X-Request-Id": "req-1"})


def flushed_log(client, bank, sync, entry=None):
    client.portal.call(bank.log.flush)
    rows = [f for _, f in sync.xrange("bank.log")]
    return [f for f in rows if entry is None or f["entry"] == entry]


def test_rules_r0_to_r5():
    reg = Registry({"card-1-0": "retail", "card-2-0": "business"})
    rules = Rules(load_spec(), reg)
    p = dict(BODY)
    assert rules.evaluate(dict(p, from_acct="lab-x")) == ("HOLD", ["R0"], None)
    assert rules.evaluate(dict(p, amount=10_001.0, ts=1.0)) == ("VERIFY", ["R1"], None)
    assert rules.evaluate(dict(p, from_acct="card-2-0", amount=50_000.0, ts=1.0)) == ("ALLOW", [], None)
    fresh = Rules(load_spec(), reg)
    out = [fresh.evaluate(dict(p, ts=1000.0 + i))[0] for i in range(10)]
    assert out == ["ALLOW"] * 9 + ["HOLD"]                                             # R2 on the 10th in 60 s
    assert Rules(load_spec(), reg).evaluate(dict(p, ts=0.0))[0] == "ALLOW"
    cards = Rules(load_spec(), reg)
    small = [cards.evaluate(dict(p, from_acct="card-2-0", amount=1.0, ts=5000.0 + 30 * i))[1] for i in range(5)]
    assert small[-1] == ["R3"] and all(r == [] for r in small[:-1])
    assert cards.evaluate(dict(p, from_acct="card-2-0", amount=900.0, ts=5200.0))[1] == ["R3"]  # still testing
    enrich = Rules(load_spec(), reg)
    assert enrich.evaluate(dict(p, ts=1.0), cluster_risk=0.85) == ("HOLD", ["R5"], None)
    assert enrich.evaluate(dict(p, ts=99999.0), edge_action="stepup") == ("VERIFY", ["R5"], None)
    assert enrich.evaluate(dict(p, amount=20_000.0, ts=500000.0), cluster_risk=0.9) == ("HOLD", ["R1", "R5"], None)


def test_channel_posts_allow_with_the_contract_shape(stack):
    client, bank, sync, tmp = stack
    sync.set("bank:bal:card-1-0", "5000")
    body = pay(client).json()
    for k, t in RESPONSE_TYPES.items():
        assert isinstance(body[k], t) and not (t is int and isinstance(body[k], bool)), k
    assert (body["action"], body["path"], body["mode"], body["engine"]["timeout"]) == ("ALLOW", "bank", "bank_only",
                                                                                         False)
    lines = (tmp / "bank" / "postings.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["via"] == "bank_channel"
    assert float(sync.get("bank:bal:card-1-0")) == 4880.0 and float(sync.get("bank:bal:merchant-9")) == 120.0
    entries = flushed_log(client, bank, sync)
    assert {e["entry"] for e in entries} == {"engine", "channel"} and all(set(e) == BANK_LOG_FIELDS for e in entries)


@pytest.mark.parametrize("mode,changes,action,posted", [
    ("open", {}, "ALLOW", True),
    ("closed", {}, "HOLD", False),
    ("bounded", {}, "ALLOW", True),                                   # known account, within the limit
    ("bounded", {"amount": 20_000.0}, "VERIFY", False),               # known account, above the limit
    ("bounded", {"from_acct": "lab-bot-1"}, "HOLD", False),           # unknown account
])
def test_timeout_fallbacks(stack, mode, changes, action, posted):
    client, bank, sync, _ = stack
    bank.settings.fail_mode, bank.settings.deadline_ms, bank.settings.service_ms = mode, 5.0, 300.0
    body = pay(client, **changes).json()
    assert (body["action"], body["deadline_met"], body["engine"]["timeout"]) == (action, False, True)
    channel = flushed_log(client, bank, sync, "channel")[-1]
    assert (channel["timeout"], channel["posted"]) == ("1", "1" if posted else "0")
    books = client.get("/bank/books").json()
    assert books["fail_open_count"] == (1 if posted else 0) and books["pending_count"] == (1 if action == "VERIFY" else 0)


def test_verify_posts_only_with_the_right_token(stack):
    client, bank, sync, tmp = stack
    assert pay(client, amount=15_000.0).json()["action"] == "VERIFY"            # R1 for a retail account
    assert client.post("/bank/verify", json={"txn_id": BODY["txn_id"], "token": "wrong"}).status_code == 403
    ok = client.post("/bank/verify", json={"txn_id": BODY["txn_id"], "token": "lab-ok"})
    assert ok.status_code == 200 and ok.json() == {"txn_id": BODY["txn_id"], "action": "ALLOW", "verified": True}
    assert client.post("/bank/verify", json={"txn_id": BODY["txn_id"], "token": "lab-ok"}).status_code == 403
    assert len((tmp / "bank" / "postings.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_settle_is_idempotent_and_posts_only_allow(stack):
    client, bank, sync, tmp = stack
    p = dict(BODY, txn_id="t-000000777", from_acct="chk-recon", to_acct="chk-recon-2", amount=1.0)
    body = {"payment": p, "action": "ALLOW", "path": "engine", "via": "shield", "req_id": "r"}
    a, b = client.post("/bank/settle", json=body).json(), client.post("/bank/settle", json=body).json()
    assert (a["posted"], a["duplicate"], b["posted"], b["duplicate"]) == (True, False, False, True)
    held = client.post("/bank/settle", json=dict(body, action="HOLD", payment=dict(p, txn_id="t-000000778"))).json()
    assert (held["posted"], held["duplicate"]) == (False, False)
    lines = (tmp / "bank" / "postings.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["via"] for x in lines] == ["shield"]
    assert client.get("/bank/books").json()["posted_count"] == 1


def test_switches_bad_bodies_and_health(stack):
    client, bank, sync, _ = stack
    assert client.post("/engine/pay", json=BODY, headers={"X-Mock-Fail": "1"}).status_code == 503
    slow = client.post("/engine/pay", json=dict(BODY, txn_id="t-000000002"), headers={"X-Mock-Delay-Ms": "60"}).json()
    assert slow["engine_latency_ms"] >= 60 and slow["decision"] in ("ALLOW", "VERIFY", "HOLD")
    assert client.post("/bank/pay", json={"txn_id": "t-1"}).status_code == 400
    assert client.post("/bank/pay", json=dict(BODY, amount=-5.0)).status_code == 400
    assert client.get("/bank/health").json()["accounts"] == 2


def test_brownout_takes_effect_within_a_second(stack):
    client, bank, sync, _ = stack
    sync.set("bank:cfg:service_ms", "40")
    deadline = time.time() + 1.2
    while bank.settings.service_ms != 40.0 and time.time() < deadline:
        time.sleep(0.05)
    assert bank.settings.service_ms == 40.0


def test_capacity_makes_requests_queue():
    async def go():
        settings = Settings(slots=1, service_ms=40.0)
        engine = Engine(Rules(load_spec(), Registry({"card-1-0": "retail"})), Capacity(1), settings,
                        BankLog(fakeredis.FakeAsyncRedis()))
        out = await asyncio.gather(*[engine.evaluate(dict(BODY, txn_id=f"t-00000000{i}", ts=float(i)), "r")
                                     for i in range(3)])
        return sorted(r.queue_ms for r in out)

    q = asyncio.run(go())
    assert q[0] < 15 and q[1] >= 35 and q[2] >= 75


def test_load_writes_balances_and_tiers(tmp_path):
    write_registry(tmp_path)
    r = fakeredis.FakeRedis(decode_responses=True)
    assert load(r, str(tmp_path)) == {"balances": 2, "tiers": 2}
    assert r.get("bank:bal:card-2-0") == "90000.0" and r.get("tier:card-2-0") == "high"
