"""python -m ledger.verify [path]: exit 0 if the whole chain is valid, else print the first bad seq and exit 1."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

from .chain import GENESIS, checkpoint_root, record_hash


def default_path() -> str:
    return os.path.join(os.environ.get("RF_DATA", "/srv/riskfabric/data"), "ledger", "ledger.jsonl")


def verify_file(path: str) -> dict:
    """Returns {"ok", "records", "head", "bad_seq", "reason"}."""
    prev, expected_seq, head = GENESIS, 1, GENESIS
    hashes_since_checkpoint: list = []
    if not os.path.exists(path):
        return {"ok": False, "records": 0, "head": None, "bad_seq": None, "reason": f"{path} not found"}
    with open(path, "rb") as fh:
        for line_no, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return _bad(expected_seq, f"line {line_no} is not valid JSON")
            seq = rec.get("seq")
            if seq != expected_seq:
                return _bad(expected_seq, f"line {line_no} has seq {seq!r}, expected {expected_seq}")
            if rec.get("prev_hash") != prev:
                return _bad(seq, "prev_hash does not match the previous record")
            stored = rec.pop("hash", None)
            if stored != record_hash(rec):
                return _bad(seq, "hash does not match the record")
            if rec.get("kind") == "checkpoint":
                if rec.get("root") != checkpoint_root(hashes_since_checkpoint):
                    return _bad(seq, "checkpoint root does not match the records it covers")
                hashes_since_checkpoint = []
            else:
                hashes_since_checkpoint.append(stored)
            prev, head = stored, stored
            expected_seq += 1
    return {"ok": True, "records": expected_seq - 1, "head": head, "bad_seq": None, "reason": None}


def _bad(seq: Optional[int], reason: str) -> dict:
    return {"ok": False, "records": None, "head": None, "bad_seq": seq, "reason": reason}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else default_path()
    result = verify_file(path)
    if result["ok"]:
        print(f"OK {result['records']} records, head {result['head']}")
        return 0
    if result["bad_seq"] is not None:
        print(f"FAIL seq {result['bad_seq']}: {result['reason']}")
    else:
        print(f"FAIL: {result['reason']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
