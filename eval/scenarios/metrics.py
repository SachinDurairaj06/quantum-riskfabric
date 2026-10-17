"""Per-run metrics M-01 to M-22 (prd/02 section 4) from the truth file, the bank's books and the tailed streams.

Pure functions over lists of dicts, so every number can be recomputed from the raw files of a run.
"""
from __future__ import annotations

import json
import math
from typing import Optional

FRAUD = ("launder", "cardtest")
ATTACK = ("bot", "cardtest")
SHED = (403, 429)
BANK_SYSTEMS = ("bank_only", "bank_realistic")  # the bank on its own: no Sentinel, no router, no ledger


def pct(values: list, p: float) -> Optional[float]:
    if not values:
        return None
    data = sorted(values)
    return round(data[min(len(data) - 1, int(math.ceil(p * len(data))) - 1)], 3)


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute(truth: list, postings: list, bank_log: list, decisions: list, threat: list, ledger_records: list,
            ledger_ok: Optional[bool], cpu_s: dict, system: str, window_start: float, deadline_ms: float = 100.0,
            hold_at: float = 0.8) -> dict:
    """truth: the run's truth lines; postings: the bank's postings for the run; the stream lists are raw entries."""
    t = [x for x in truth if x["ts_sent"] >= window_start]
    posted = {p["txn_id"]: p for p in postings}
    by_txn_bank: dict = {}
    for e in bank_log:
        by_txn_bank.setdefault(e.get("txn_id"), []).append(e)

    fraud = [x for x in t if x["kind"] in FRAUD]
    legit = [x for x in t if x["kind"] == "legit"]
    attack = [x for x in t if x["kind"] in ATTACK]
    fraud_through = [x for x in fraud if x["txn_id"] in posted]
    # M-04: value the bank channel posted through its timeout fallback (fail-open, or bounded in bank_realistic)
    fail_open_value = 0.0
    for txn_id, p in posted.items():
        if any(e.get("entry") == "channel" and e.get("timeout") == "1" for e in by_txn_bank.get(txn_id, [])):
            fail_open_value += float(p.get("amount", 0))
    attack_reached = sum(1 for x in attack if any(e.get("entry") in ("engine", "channel") for e in by_txn_bank.get(x["txn_id"], [])))

    ok_lat = [x["rtt_ms"] for x in legit if x["status"] == 200]
    misses = sum(1 for x in legit if x["status"] == 0 or (x["status"] == 200 and x["rtt_ms"] > deadline_ms))
    engine_lat = [_f(e.get("latency_ms")) for e in bank_log if e.get("entry") == "engine" and _f(e.get("ts"), 0) >= window_start]
    engine_lat = [v for v in engine_lat if v is not None]

    # time to detect: first acted-on cluster whose key belongs to the attack's fingerprints
    bot_fps = {x["fp"] for x in truth if x["kind"] == "bot"}
    bot_start = min((x["ts_sent"] for x in truth if x["kind"] == "bot"), default=None)
    ttd = None
    if bot_fps and bot_start is not None and system not in BANK_SYSTEMS:
        for e in sorted(threat, key=lambda e: _f(e.get("ts"), 0)):
            ts = _f(e.get("ts"), 0)
            if ts < bot_start - 1:
                continue
            clusters = e.get("clusters") or []
            if isinstance(clusters, str):
                try:
                    clusters = json.loads(clusters)
                except ValueError:
                    clusters = []
            if any(c.get("action") and c.get("key", "").split("|", 1)[0] in bot_fps for c in clusters):
                ttd = round(max(0.0, ts + 1 - bot_start), 2)  # the entry describes the window [ts, ts + 1)
                break
    false_coord = sum(1 for e in threat if e.get("level") == "COORDINATED" and _f(e.get("ts"), 0) >= window_start)

    expected = {x["txn_id"] for x in t if x["action"] == "ALLOW" or (x["action"] == "VERIFY" and x["verified"])}
    actual = {x["txn_id"] for x in t if x["txn_id"] in posted}
    mismatch = len(expected ^ actual)

    shadow = {r["txn_id"]: _f(r.get("shadow_score")) for r in ledger_records if r.get("kind") == "shadow"}
    uplift = None
    if system == "shield":
        f_hold = [x for x in fraud_through if (shadow.get(x["txn_id"]) or 0) >= hold_at]
        l_hold = [x for x in legit if (shadow.get(x["txn_id"]) or 0) >= hold_at]
        uplift = {"fraud_count": len(f_hold), "fraud_value": round(sum(x["amount"] for x in f_hold), 2),
                  "legit_would_hold_per_1000": round(1000 * len(l_hold) / len(legit), 3) if legit else None}

    n_req = len(t)
    n_dec = sum(1 for d in decisions if _f(d.get("ts"), 0) >= window_start)
    n_bank = sum(1 for e in bank_log if e.get("entry") in ("engine", "channel") and _f(e.get("ts"), 0) >= window_start)
    n_hold = sum(1 for d in decisions if d.get("action") == "HOLD" and _f(d.get("ts"), 0) >= window_start)
    denominators = {"edge": n_req, "decision": n_dec, "sentinel": n_req, "bank": n_bank, "quantum": n_hold}
    cpu = {k: (round(1e6 * cpu_s[k] / denominators[k], 2) if cpu_s.get(k) is not None and denominators[k] else None)
           for k in denominators}

    fraud_sent = sum(x["amount"] for x in fraud)
    fraud_value_through = sum(x["amount"] for x in fraud_through)
    completed = sum(1 for x in legit if x["txn_id"] in posted)
    miss_rate = misses / len(legit) if legit else None
    protection = round(100 * (1 - fraud_value_through / fraud_sent), 3) if fraud_sent else None
    customer = round(100 * completed / len(legit), 3) if legit else None
    timeliness = round(100 * (1 - miss_rate), 3) if miss_rate is not None else None
    integrity = None if system in BANK_SYSTEMS else (100 if (ledger_ok and mismatch == 0) else 0)
    parts = [v for v in (protection, customer, timeliness) if v is not None]
    return {
        "fraud_value_sent": round(fraud_sent, 2), "fraud_value_through": round(fraud_value_through, 2),
        "fraud_count_sent": len(fraud), "fraud_count_through": len(fraud_through), "fail_open_value": round(fail_open_value, 2),
        "attack_sent": len(attack), "attack_shed": sum(1 for x in attack if x["status"] in SHED), "attack_reached_bank": attack_reached,
        "legit_sent": len(legit), "legit_completed": completed, "legit_held": sum(1 for x in legit if x["action"] == "HOLD"),
        "legit_blocked": sum(1 for x in legit if x["status"] in SHED), "legit_verify": sum(1 for x in legit if x["action"] == "VERIFY"),
        "p50_ms": pct(ok_lat, 0.5), "p95_ms": pct(ok_lat, 0.95), "p99_ms": pct(ok_lat, 0.99),
        "deadline_miss_rate": round(miss_rate, 6) if miss_rate is not None else None,
        "bank_engine_p99_ms": pct(engine_lat, 0.99), "bank_timeouts": sum(1 for e in bank_log if e.get("timeout") == "1"),
        "stand_in_count": sum(1 for d in decisions if d.get("stand_in") == "1" and _f(d.get("ts"), 0) >= window_start),
        "time_to_detect_s": ttd, "false_coordinated_s": false_coord,
        "ledger_ok": None if system in BANK_SYSTEMS else ledger_ok,
        "reconcile_mismatch": mismatch, "shadow_uplift": uplift, "cpu_us_per_req": cpu,
        "scores": {"protection": protection, "customer": customer, "timeliness": timeliness, "integrity": integrity,
                   "shield_score": round(sum(parts) / len(parts), 3) if parts else None},
    }


def flatten(result: dict) -> dict:
    """Flat metric names used by summary.json and the scoreboard (derived rates included)."""
    out = {k: result.get(k) for k in ("fraud_value_sent", "fraud_value_through", "fraud_count_through", "fail_open_value",
                                      "attack_reached_bank", "p50_ms", "p95_ms", "p99_ms", "deadline_miss_rate",
                                      "bank_engine_p99_ms", "bank_timeouts", "stand_in_count", "time_to_detect_s",
                                      "false_coordinated_s", "reconcile_mismatch")}
    out["ledger_ok"] = result.get("ledger_ok")
    a, l = result.get("attack_sent") or 0, result.get("legit_sent") or 0
    out["attack_shed_share"] = (result["attack_shed"] / a) if a else None
    out["legit_completed_rate"] = (result["legit_completed"] / l) if l else None
    out["legit_held_per_1000"] = (1000 * result["legit_held"] / l) if l else None
    out["legit_blocked_per_1000"] = (1000 * result["legit_blocked"] / l) if l else None
    out["legit_verify_rate"] = (result["legit_verify"] / l) if l else None
    for k, v in (result.get("cpu_us_per_req") or {}).items():
        out[f"cpu_{k}"] = v
    up = result.get("shadow_uplift") or {}
    out["shadow_fraud_count"], out["shadow_fraud_value"] = up.get("fraud_count"), up.get("fraud_value")
    out["shadow_legit_would_hold_per_1000"] = up.get("legit_would_hold_per_1000")
    out.update(result.get("scores") or {})
    return out
