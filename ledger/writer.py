"""python -m ledger.writer: the single ledger writer (prd/07 E-11, CONTRACTS section 8).

Reads `ledger.in`, appends hash-chained records to $RF_DATA/ledger/ledger.jsonl, writes a checkpoint
every 1000 records and keeps `ledger:head` current. Its stream position lives in `ledger:cursor`
(an E-private key) so a restart never appends an entry twice.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional

from .chain import CHECKPOINT_EVERY, GENESIS, checkpoint_root, seal

log = logging.getLogger("ledger.writer")
CURSOR_KEY = "ledger:cursor"
HEAD_KEY = "ledger:head"


def ledger_path() -> str:
    return os.path.join(os.environ.get("RF_DATA", "/srv/riskfabric/data"), "ledger", "ledger.jsonl")


class LedgerWriter:
    def __init__(self, redis, path: str):
        self.r = redis
        self.path = path
        self.seq, self.prev = 0, GENESIS
        self.since_checkpoint: list = []
        self.range_start = 1
        self._recover()

    def _recover(self) -> None:
        """Continue an existing chain from its last line."""
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            return
        last: Optional[dict] = None
        with open(self.path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                rec = json.loads(raw)
                last = rec
                if rec.get("kind") == "checkpoint":
                    self.since_checkpoint = []
                    self.range_start = rec["seq"] + 1
                else:
                    self.since_checkpoint.append(rec["hash"])
        if last is not None:
            self.seq, self.prev = last["seq"], last["hash"]

    def start_id(self) -> str:
        cursor = self.r.get(CURSOR_KEY)
        if cursor:
            return cursor
        if self.seq > 0:
            log.warning("ledger has %d records but no cursor; reading only new ledger.in entries", self.seq)
            newest = self.r.xrevrange("ledger.in", count=1)
            return newest[0][0] if newest else "0-0"
        return "0"

    def append(self, records: list) -> None:
        """Seal and append records (plus checkpoints) in one write and fsync."""
        lines = []
        for rec in records:
            self.seq += 1
            sealed = seal(rec, self.seq, self.prev)
            self.prev = sealed["hash"]
            self.since_checkpoint.append(sealed["hash"])
            lines.append(sealed)
            if len(self.since_checkpoint) >= CHECKPOINT_EVERY:
                self.seq += 1
                cp = seal({"ts": time.time(), "kind": "checkpoint", "seq_range": [self.range_start, self.seq - 1],
                           "root": checkpoint_root(self.since_checkpoint), "sig": None}, self.seq, self.prev)
                self.prev = cp["hash"]
                self.since_checkpoint = []
                self.range_start = self.seq + 1
                lines.append(cp)
        with open(self.path, "ab") as fh:
            for line in lines:
                fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def step(self, last_id: str, block_ms: int = 1000) -> str:
        resp = self.r.xread({"ledger.in": last_id}, count=1000, block=block_ms)
        if not resp:
            return last_id
        records, new_id = [], last_id
        for _, entries in resp:
            for entry_id, fields in entries:
                new_id = entry_id
                try:
                    records.append(json.loads(fields.get("rec", "")))
                except ValueError:
                    log.error("ledger.in entry %s is not JSON; recorded as a malformed entry", entry_id)
                    records.append({"ts": time.time(), "kind": "malformed", "source_id": entry_id})
        self.append(records)
        pipe = self.r.pipeline(transaction=False)
        pipe.set(HEAD_KEY, self.prev)
        pipe.set(CURSOR_KEY, new_id)
        pipe.execute()
        return new_id

    def run(self) -> None:
        last_id = self.start_id()
        log.info("ledger writer: %s from %s (seq %d)", self.path, last_id, self.seq)
        while True:
            try:
                last_id = self.step(last_id)
            except Exception:
                log.exception("ledger writer step failed; retrying in 1 s")
                time.sleep(1.0)


def main() -> int:
    import redis

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    r = redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    LedgerWriter(r, ledger_path()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
