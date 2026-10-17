"""The engine's fixed capacity (prd/03 section 4.3): `slots` requests at a time, the rest wait their turn.

Capacity is about `slots x 1000 / service_ms` requests per second. That is why the bank alone breaks under an attack:
abusive traffic takes slots that customers need.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


class Capacity:
    def __init__(self, slots: int):
        self.limit = max(1, int(slots))
        self.in_use = 0
        self.waiting = 0
        self._cond = asyncio.Condition()

    async def resize(self, slots: int) -> None:
        slots = max(1, int(slots))
        if slots != self.limit:
            async with self._cond:
                self.limit = slots
                self._cond.notify_all()

    @asynccontextmanager
    async def slot(self):
        """Holds one slot; yields how long this request queued for it, in ms."""
        t0 = time.perf_counter()
        async with self._cond:
            self.waiting += 1
            try:
                await self._cond.wait_for(lambda: self.in_use < self.limit)
            finally:
                self.waiting -= 1
            self.in_use += 1
        try:
            yield (time.perf_counter() - t0) * 1000.0
        finally:
            async with self._cond:
                self.in_use -= 1
                self._cond.notify()
