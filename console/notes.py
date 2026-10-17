"""Templated case notes (prd/07 E-16): a plain sentence from reason codes, tier, cluster and amount. No LLM."""
from __future__ import annotations

import json
from typing import Optional


def _reasons(raw) -> list:
    if isinstance(raw, list):
        return [str(r) for r in raw]
    try:
        value = json.loads(raw or "[]")
        return [str(r) for r in value] if isinstance(value, list) else []
    except ValueError:
        return []


def case_note(hold: dict) -> str:
    reasons = [r for r in _reasons(hold.get("reasons")) if not r.startswith("cluster ")]
    stand_in = hold.get("stand_in") in ("1", True, "true")
    parts = []
    if stand_in:
        parts.append("Held by stand-in rules because the check was late")
    elif reasons:
        parts.append("Held: " + ", ".join(reasons[:3]))
    else:
        parts.append("Held by the risk policy")
    tier: Optional[str] = hold.get("tier") or None
    if tier:
        parts.append(f"account tier {tier}")
    if hold.get("cluster"):
        parts.append(f"part of cluster {hold['cluster']}")
    try:
        amount = float(hold.get("amount"))
        parts.append(f"amount {amount:,.2f} {hold.get('currency') or ''}".rstrip())
    except (TypeError, ValueError):
        pass
    return "; ".join(parts) + "."
