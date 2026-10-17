"""Calls to the synthetic bank on 127.0.0.1:9000 (CONTRACTS section 11)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("decision.bank")

ACTIONS = ("ALLOW", "VERIFY", "HOLD")


class EngineUnavailable(Exception):
    """The engine timed out, failed or answered something we can't use."""


class BankClient:
    def __init__(self, base_url: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(2.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def engine_decision(self, payment: dict, headers: dict, budget_ms: float) -> dict:
        """POST /engine/pay within the budget. Returns the engine answer plus our measured latency."""
        if budget_ms <= 0:
            raise EngineUnavailable("no budget left")
        start = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self.client.post("/engine/pay", json=payment, headers=headers), timeout=budget_ms / 1000.0
            )
        except asyncio.TimeoutError:
            raise EngineUnavailable("timeout")
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"http error: {exc.__class__.__name__}")
        latency_ms = (time.perf_counter() - start) * 1000.0
        if resp.status_code >= 500:
            raise EngineUnavailable(f"status {resp.status_code}")
        if resp.status_code != 200:
            raise EngineUnavailable(f"unexpected status {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise EngineUnavailable("not json")
        decision = body.get("decision") or body.get("action")
        if decision not in ACTIONS:
            raise EngineUnavailable(f"bad decision {decision!r}")
        return {
            "decision": decision,
            "score": body.get("score"),
            "reasons": list(body.get("reasons") or [])[:5],
            "latency_ms": round(latency_ms, 3),
        }

    async def settle(self, payment: dict, action: str, path: str, via: str, req_id: str) -> bool:
        """POST /bank/settle, retried up to 3 times within about 2 s. Idempotent at the bank."""
        body = {"payment": payment, "action": action, "path": path, "via": via, "req_id": req_id}
        for attempt, pause in enumerate((0.0, 0.3, 0.7)):
            if pause:
                await asyncio.sleep(pause)
            try:
                resp = await self.client.post("/bank/settle", json=body, timeout=0.5)
                if resp.status_code == 200:
                    return True
                log.warning("settle %s attempt %d: status %d", payment["txn_id"], attempt + 1, resp.status_code)
            except httpx.HTTPError as exc:
                log.warning("settle %s attempt %d: %s", payment["txn_id"], attempt + 1, exc.__class__.__name__)
        return False
