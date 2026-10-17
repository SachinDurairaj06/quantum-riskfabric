"""Score thresholds and the stand-in policy (CONTRACTS section 7)."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("decision.policy")

DEFAULT_POLICY = {"version": "pol-v1", "verify_at": 0.4, "hold_at": 0.8}
TIERS = ("low", "medium", "high")


def load_policy(path: Optional[str]) -> dict:
    """Read scoring/policy.json. Missing file: the contract's example values, marked as defaults."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            pol = json.load(fh)
        for key in ("version", "verify_at", "hold_at"):
            if key not in pol:
                raise ValueError(f"{path} is missing {key!r}")
        if not 0 <= float(pol["verify_at"]) <= float(pol["hold_at"]) <= 1:
            raise ValueError(f"{path}: need 0 <= verify_at <= hold_at <= 1")
        return pol
    log.warning("policy file %s not found; using contract defaults", path)
    return dict(DEFAULT_POLICY, version="pol-default")


def action_from_score(score: float, policy: dict, edge_action: Optional[str]) -> str:
    if score >= float(policy["hold_at"]):
        action = "HOLD"
    elif score >= float(policy["verify_at"]):
        action = "VERIFY"
    else:
        action = "ALLOW"
    if edge_action == "stepup" and action == "ALLOW":
        action = "VERIFY"
    return action


def normalise_tier(raw: Optional[str]) -> str:
    return raw if raw in TIERS else "unknown"


def stand_in(tier: str, amount: float, low_limit: float) -> str:
    if tier == "low":
        return "ALLOW" if amount <= low_limit else "VERIFY"
    if tier == "medium":
        return "VERIFY"
    return "HOLD"  # high or unknown


def stand_in_reasons(tier: str, mode: str) -> list:
    what = "engine" if mode == "shield" else "scoring"
    return [f"stand-in: {what} unavailable", f"tier {tier}"]
