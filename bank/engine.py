"""The incumbent fraud engine (prd/03 section 4.3): rules R0 to R5 behind the fixed-capacity queue.

Each request holds a slot for its rule time plus `bank:cfg:service_ms`, an awaited sleep, so the model of the bank's
sizing doesn't burn the VM's CPU. Requests that find no free slot wait. A caller that gives up (the channel at its
deadline, or the Shield router at its budget) doesn't free the slot: the engine finishes the work, as a real one would.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


async def hold(ms: float) -> None:
    """Sleeps at least `ms`. asyncio may wake a timer up to one clock tick early (about 15 ms on Windows)."""
    target = time.perf_counter() + ms / 1000.0
    while (left := target - time.perf_counter()) > 0:
        await asyncio.sleep(left)


@dataclass
class EngineResult:
    action: str
    rules: list
    score: Optional[float]
    latency_ms: float
    queue_ms: float


class Engine:
    def __init__(self, rules, capacity, settings, banklog):
        self.rules, self.capacity, self.settings, self.log = rules, capacity, settings, banklog

    async def evaluate(self, p: dict, req_id: str, cluster_risk: float = 0.0, edge_action: Optional[str] = None,
                       extra_ms: float = 0.0) -> EngineResult:
        t0 = time.perf_counter()
        async with self.capacity.slot() as queue_ms:
            await hold(self.settings.service_ms + extra_ms)
            action, fired, score = self.rules.evaluate(p, cluster_risk, edge_action)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self.log.add(txn_id=p["txn_id"], req_id=req_id, entry="engine", action=action, latency_ms=latency_ms,
                     queue_ms=queue_ms, rules=fired)
        return EngineResult(action, fired, score, round(latency_ms, 3), round(queue_ms, 3))
