"""Sentinel window tests with synthetic traffic shaped like the lab's (prd/04 SN-03 to SN-05, check C-09)."""
import random

import fakeredis

from lab.identities import Population
from sentinel import rules as R
from sentinel.detector import Sentinel
from sentinel.worker import run

POP = Population(5)
CUSTOMERS = [POP.customer(f"acct{i}") for i in range(3000)]


def feed_second(det, t, legit_rps, bots=None, bot_rps=0, verify_bots=False, rng=random.Random(1)):
    n = 0
    for _ in range(legit_rps):
        c = rng.choice(CUSTOMERS)
        rid = f"r{t}-{n}"
        n += 1
        det.edge({"ts": str(t + rng.random()), "req_id": rid, "ip": c.ip, "fp": c.fp, "asn": str(c.asn),
                  "path": "/pay", "status": "200", "known": "1" if c.known else "0"})
        action = "VERIFY" if rng.random() < 0.05 else "ALLOW"
        det.decision({"req_id": rid, "action": action})
        if action == "VERIFY" and rng.random() < 0.97:
            det.edge({"ts": str(t + 0.5), "req_id": rid + "v", "ip": c.ip, "fp": c.fp, "asn": str(c.asn),
                      "path": "/verify", "status": "200"})
    if bots:
        start = (t * bot_rps) % len(bots)
        for j in range(bot_rps):
            b = bots[(start + j) % len(bots)]
            rid = f"b{t}-{j}"
            det.edge({"ts": str(t + j / bot_rps), "req_id": rid, "ip": b.ip, "fp": b.fp, "asn": str(b.asn),
                      "path": "/pay", "status": "200", "known": "0"})
            det.decision({"req_id": rid, "action": "HOLD"})
    return det.close(float(t))


def test_coordinated_cluster_detected_within_10_seconds_and_escalates_then_releases():
    det = Sentinel()
    for t in range(60):
        assert feed_second(det, t, 200)["level"] == "NORMAL"
    bots = POP.bots(2000, 1, 3)
    first_action, actions = None, []
    for t in range(60, 90):
        entry = feed_second(det, t, 200, bots, 800)
        if entry["clusters"] and first_action is None:
            first_action = t
        actions.append(entry["clusters"][0]["action"] if entry["clusters"] else None)
    assert first_action is not None and first_action - 60 <= 10
    assert entry["level"] == "COORDINATED"
    c = entry["clusters"][0]
    assert c["key_type"] == "fp" and c["key"] == bots[0].fp and 0 < c["risk"] <= 1 and c["until"] > 89
    assert "throttle" in actions and "stepup" in actions and actions[-1] == "block"
    for t in range(90, 105):
        entry = feed_second(det, t, 200)
    assert entry["clusters"] == [] and entry["level"] == "NORMAL"


def test_payday_surge_is_never_coordinated():
    det = Sentinel()
    for t in range(60):
        feed_second(det, t, 200)
    levels = [feed_second(det, t, 600)["level"] for t in range(60, 150)]
    assert "COORDINATED" not in levels
    assert levels.count("SURGE") > 80
    assert all(not e for e in [det.clusters])


def test_bots_using_the_bank_app_fingerprint_get_fp_asn_and_at_most_stepup():
    det = Sentinel()
    for t in range(60):
        feed_second(det, t, 200)
    bots = POP.bots(2000, 1, 3, use_app_fp=True)
    for t in range(60, 100):
        entry = feed_second(det, t, 200, bots, 800)
    kinds = {c["key_type"] for c in entry["clusters"]}
    assert kinds == {"fp_asn"}
    assert all(c["action"] in ("throttle", "stepup") for c in entry["clusters"])
    assert all(c["key"].startswith(POP.app_fingerprint + "|") for c in entry["clusters"])


def test_the_app_fingerprint_stays_protected_at_a_low_customer_rate():
    """At 10 customer payments a second the app fingerprint sends only about 4 a window, below MIN_COUNT. Its normal
    customer mix must still be learned, so bots that borrow it get fp_asn clusters and the bare fingerprint is never
    blocked."""
    det = Sentinel()
    for t in range(60):
        feed_second(det, t, 10)
    bots = POP.bots(2000, 1, 3, use_app_fp=True)
    seen = []
    for t in range(60, 100):
        seen += feed_second(det, t, 10, bots, 40)["clusters"]
    assert seen and {c["key_type"] for c in seen} == {"fp_asn"}
    assert all(c["action"] in ("throttle", "stepup") for c in seen)


def test_worker_writes_one_threat_entry_per_second():
    r = fakeredis.FakeRedis(decode_responses=True)
    t = [1000.0]

    def clock():
        t[0] += 0.05
        return t[0]

    run(r, clock=clock, max_windows=5)
    entries = r.xrange("threat")
    assert len(entries) == 5
    assert [int(e[1]["seq"]) for e in entries] == [1, 2, 3, 4, 5]
    assert all(e[1]["level"] == "NORMAL" and e[1]["clusters"] == "[]" for e in entries)


def test_rules_weights_sum_to_one():
    assert abs(R.W_JUMP + R.W_UNKNOWN + R.W_VERIFY + R.W_REGULAR + R.W_ASN - 1.0) < 1e-9
