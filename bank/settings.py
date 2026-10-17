"""The bank's live settings, the `bank:cfg:*` keys (CONTRACTS section 6).

They are read at startup and again every 0.5 s, so C's brownout in S5 (a new `bank:cfg:service_ms`) takes effect
within 1 s (BK-10). A value that doesn't parse is ignored and logged; the last good value stays.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("bank.settings")

KEYS = {"slots": "bank:cfg:slots", "service_ms": "bank:cfg:service_ms", "deadline_ms": "bank:cfg:deadline_ms",
        "fail_mode": "bank:cfg:fail_mode", "fallback_limit": "bank:cfg:fallback_limit"}
FAIL_MODES = ("open", "closed", "bounded")
REFRESH_S = 0.5


@dataclass
class Settings:
    slots: int = 8
    service_ms: float = 4.0
    deadline_ms: float = 100.0
    fail_mode: str = "open"
    fallback_limit: float = 10000.0

    def apply(self, raw: dict) -> None:
        for name, value in raw.items():
            if value is None:
                continue
            try:
                if name == "slots":
                    parsed = max(1, int(float(value)))
                elif name == "fail_mode":
                    if value not in FAIL_MODES:
                        raise ValueError(value)
                    parsed = value
                else:
                    parsed = float(value)
                    if parsed < 0:
                        raise ValueError(value)
            except ValueError:
                log.warning("ignoring %s=%r", KEYS[name], value)
                continue
            setattr(self, name, parsed)


async def read(r, settings: Settings) -> None:
    values = await r.mget(list(KEYS.values()))
    settings.apply(dict(zip(KEYS, values)))


async def refresh_forever(r, settings: Settings, capacity) -> None:
    while True:
        await asyncio.sleep(REFRESH_S)
        try:
            await read(r, settings)
            await capacity.resize(settings.slots)
        except Exception:  # keep serving with the last good settings
            log.exception("could not read bank:cfg:*")
