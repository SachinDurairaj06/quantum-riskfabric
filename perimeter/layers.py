"""The three layers a bank's traffic passes before it reaches the bank, as one local stand-in for the demo.

1. **CDN and DDoS layer, Cloudflare-style.** An IP blocklist (403) and a per-IP request rate (429 above it).
2. **WAF, AWS-WAF-style managed rules.** Only the bank's API routes, JSON only, at most 8 KB, and signatures for SQL
   injection, script injection and path traversal in any string value (403).
3. **API gateway, API-Gateway-style.** A global ceiling on requests per second (429 above it), then the request
   schema (400).

None of the three sees payment outcomes or links identities. A bot that stays under the per-IP rate and sends
well-formed JSON passes all of them, and that gap is what RiskFabric's edge and Sentinel fill.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional

ROUTES = {("POST", "/pay"), ("POST", "/verify")}
MAX_BODY = 8192
DEFAULT_IP_RPS = 20.0
SIGNATURES = {
    "sql injection": re.compile(r"(?i)(\bunion\b[\s\S]{0,40}\bselect\b|\bor\b\s+\d+\s*=\s*\d+|'\s*or\s*'"
                                r"|;\s*(drop|delete|insert|update)\b|/\*|--(\s|$))"),
    "script injection": re.compile(r"(?i)(<\s*script|javascript\s*:|on(error|load)\s*=)"),
    "path traversal": re.compile(r"\.\./|\.\.\\"),
}
# any sane id: a real gateway checks the shape, not the lab's own numbering, so other traffic sources (the attack
# simulator uses ids like sim-...-5) pass too. The bank and edge already accept any string id.
TXN_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Policy:
    ip_rps: float = DEFAULT_IP_RPS
    ceiling_rps: Optional[float] = None       # None: no ceiling until the capacity is measured
    blocklist: frozenset = field(default_factory=frozenset)


class RateWindow:
    """Requests per key in the current one-second window."""

    def __init__(self):
        self.second, self.counts = 0, {}

    def hit(self, key: str, now: float) -> int:
        sec = int(now)
        if sec != self.second:
            self.second, self.counts = sec, {}
        n = self.counts.get(key, 0) + 1
        self.counts[key] = n
        return n


class TokenBucket:
    """`rate` requests per second with a burst of one second's worth."""

    def __init__(self, rate: float, now: float):
        self.rate, self.tokens, self.last = rate, rate, now

    def take(self, now: float) -> bool:
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def client_ip(headers) -> str:
    """The simulated client address: the lab sets it in X-Forwarded-For (CONTRACTS section 3)."""
    return (headers.get("x-forwarded-for") or "").split(",")[0].strip() or "unknown"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def schema_error(path: str, body: dict) -> Optional[str]:
    if path == "/verify":
        if not (isinstance(body.get("txn_id"), str) and TXN_ID.match(body["txn_id"])):
            return "txn_id must be 1-64 chars of letters, digits, dot, underscore or hyphen"
        return None if isinstance(body.get("token"), str) and len(body["token"]) <= 64 else "token must be a string"
    if not (isinstance(body.get("txn_id"), str) and TXN_ID.match(body["txn_id"])):
        return "txn_id must look like t-000000000"
    if not _number(body.get("ts")):
        return "ts must be a number"
    for name in ("from_acct", "to_acct"):
        if not (isinstance(body.get(name), str) and 0 < len(body[name]) <= 64):
            return f"{name} must be a string of 1 to 64 characters"
    if not (_number(body.get("amount")) and 0 < body["amount"] <= 1e9):
        return "amount must be a positive number"
    if not (isinstance(body.get("currency"), str) and CURRENCY.match(body["currency"])):
        return "currency must be three capital letters"
    if not (isinstance(body.get("format"), str) and 0 < len(body["format"]) <= 32):
        return "format must be a string of 1 to 32 characters"
    return None


class Perimeter:
    def __init__(self, policy: Policy = Policy()):
        self.policy = policy
        self.per_ip = RateWindow()
        self.bucket: Optional[TokenBucket] = None
        self.received = 0
        self.passed = 0
        self.stopped: dict = {"cdn": {}, "waf": {}, "gateway": {}}

    def set_policy(self, policy: Policy, now: float) -> None:
        if policy.ceiling_rps != self.policy.ceiling_rps or (policy.ceiling_rps and self.bucket is None):
            self.bucket = TokenBucket(policy.ceiling_rps, now) if policy.ceiling_rps else None
        self.policy = policy

    def stop(self, layer: str, reason: str, status: int) -> tuple:
        self.stopped[layer][reason] = self.stopped[layer].get(reason, 0) + 1
        return status, layer, reason

    def check(self, method: str, path: str, headers, body: bytes, now: float) -> Optional[tuple]:
        """None if the request may pass, else (status, layer, reason)."""
        self.received += 1
        ip = client_ip(headers)
        if ip in self.policy.blocklist:
            return self.stop("cdn", "blocklisted IP", 403)
        if self.per_ip.hit(ip, now) > self.policy.ip_rps:
            return self.stop("cdn", "per-IP rate limit", 429)
        if (method, path) not in ROUTES:
            return self.stop("waf", "route not allowed", 403)
        if "application/json" not in (headers.get("content-type") or ""):
            return self.stop("waf", "not JSON", 403)
        if len(body) > MAX_BODY:
            return self.stop("waf", "body too large", 403)
        try:
            parsed = json.loads(body)
        except ValueError:
            return self.stop("waf", "malformed JSON", 403)
        if not isinstance(parsed, dict):
            return self.stop("waf", "malformed JSON", 403)
        for text in _strings(parsed):
            for name, sig in SIGNATURES.items():
                if sig.search(text):
                    return self.stop("waf", name, 403)
        if self.bucket is not None and not self.bucket.take(now):
            return self.stop("gateway", "capacity ceiling", 429)
        err = schema_error(path, parsed)
        if err:
            return self.stop("gateway", err, 400)
        self.passed += 1
        return None

    def snapshot(self) -> dict:
        return {"received": self.received, "passed": self.passed, "stopped": self.stopped,
                "policy": {"ip_rps": self.policy.ip_rps, "ceiling_rps": self.policy.ceiling_rps,
                           "blocklisted_ips": len(self.policy.blocklist)}}
