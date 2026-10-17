"""The hash rule (CONTRACTS section 8), shared by the writer and the verifier."""
from __future__ import annotations

import hashlib
import json
import math

GENESIS = "0" * 64
CHECKPOINT_EVERY = 1000


def clean(value):
    """Replace NaN and infinities with None, recursively (canonical JSON has no NaN)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def record_hash(record_without_hash: dict) -> str:
    """hex SHA-256 of prev_hash followed by the canonical JSON of the record without `hash`."""
    prev = record_without_hash["prev_hash"]
    return hashlib.sha256(prev.encode("ascii") + canonical(record_without_hash)).hexdigest()


def seal(record: dict, seq: int, prev_hash: str) -> dict:
    body = clean(dict(record))
    body.pop("hash", None)
    body["seq"] = seq
    body["prev_hash"] = prev_hash
    body["hash"] = record_hash(body)
    return body


def checkpoint_root(hashes: list) -> str:
    """Root over a checkpoint range: SHA-256 of the concatenated record hashes, in order."""
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
