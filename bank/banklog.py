"""The `bank.log` stream (CONTRACTS section 5). Entries are buffered and written every 20 ms, so no payment waits.

`entry` is `channel`, `engine`, `verify` or `settle`; `latency_ms` includes `queue_ms`; `rules` is the comma-separated
list of rule ids that fired; `timeout` and `posted` are `1` or `0`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("bank.log")

STREAM = "bank.log"
MAXLEN = 100_000
FLUSH_S = 0.02


class BankLog:
    def __init__(self, r, flush_s: float = FLUSH_S):
        self.r, self.flush_s = r, flush_s
        self.buffer: list = []

    def add(self, *, txn_id: str, req_id: Optional[str], entry: str, action: Optional[str], latency_ms: float = 0.0,
            queue_ms: float = 0.0, timeout: bool = False, rules=(), posted: bool = False) -> None:
        self.buffer.append({"ts": f"{time.time():.6f}", "txn_id": txn_id, "req_id": req_id or "", "entry": entry,
                            "action": action or "", "latency_ms": f"{latency_ms:.3f}", "queue_ms": f"{queue_ms:.3f}",
                            "timeout": "1" if timeout else "0", "rules": ",".join(rules), "posted": "1" if posted else "0"})

    async def flush(self) -> None:
        batch, self.buffer = self.buffer, []
        if not batch:
            return
        pipe = self.r.pipeline(transaction=False)
        for fields in batch:
            pipe.xadd(STREAM, fields, maxlen=MAXLEN, approximate=True)
        await pipe.execute()

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.flush_s)
            try:
                await self.flush()
            except Exception:  # that batch is lost; the books still have every posting
                log.exception("bank.log write failed")
