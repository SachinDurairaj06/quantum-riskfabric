"""The perimeter stand-in on 127.0.0.1:8000 (plus RF_PORT_OFFSET).

Each request goes through the layers in perimeter/layers.py, then on, unchanged, to the edge on 8080: RiskFabric's
edge, or the bank's own edge for `bank_only` and `bank_realistic`. Policy is re-read from Valkey every second:
`cfg:perimeter_ip_rps` (default 20), `cfg:perimeter_limit_rps` (set by `python -m perimeter limit` from the measured
capacity; absent means no ceiling) and the set `perimeter:blocklist`. `GET /perimeter/stats` counts what each layer
stopped, so the demo can show how much bot traffic the perimeter let through.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .layers import DEFAULT_IP_RPS, MAX_BODY, Perimeter, Policy

log = logging.getLogger("perimeter")

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection",
       "te", "trailer"}
PASS_BACK = ("content-type", "retry-after")
REFRESH_S = 1.0


async def read_capped(request: Request) -> Optional[bytes]:
    """The body, or None as soon as it passes MAX_BODY: an oversized body is never buffered in full."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def read_policy(r) -> Policy:
    ip, ceiling = await r.mget("cfg:perimeter_ip_rps", "cfg:perimeter_limit_rps")
    return Policy(ip_rps=float(ip) if ip else DEFAULT_IP_RPS, ceiling_rps=float(ceiling) if ceiling else None,
                  blocklist=frozenset(await r.smembers("perimeter:blocklist")))


async def refresh_forever(r, perimeter: Perimeter) -> None:
    while True:
        try:
            perimeter.set_policy(await read_policy(r), time.time())
        except Exception:  # keep the last good policy
            log.exception("could not read the perimeter policy")
        await asyncio.sleep(REFRESH_S)


def create_app(redis=None, upstream: Optional[str] = None, transport=None) -> FastAPI:
    offset = int(os.environ.get("RF_PORT_OFFSET", "0"))
    upstream = upstream or f"http://127.0.0.1:{8080 + offset}"
    perimeter = Perimeter()
    client = httpx.AsyncClient(base_url=upstream, transport=transport, timeout=httpx.Timeout(5.0),
                               limits=httpx.Limits(max_connections=500, max_keepalive_connections=200))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(refresh_forever(redis, perimeter)) if redis is not None else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            await client.aclose()

    app = FastAPI(title="Perimeter stand-in", lifespan=lifespan)
    app.state.perimeter = perimeter

    @app.get("/perimeter/stats")
    async def stats():
        return perimeter.snapshot()

    @app.get("/perimeter/health")
    async def health():
        return {"ok": True, "upstream": upstream}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def gate(path: str, request: Request):
        body = await read_capped(request)
        if body is None:
            perimeter.received += 1
            verdict = perimeter.stop("waf", "body too large", 403)
        else:
            verdict = perimeter.check(request.method, "/" + path, request.headers, body, time.time())
        if verdict:
            status, layer, reason = verdict
            return JSONResponse({"error": reason, "layer": layer}, status_code=status,
                                headers={"Retry-After": "1"} if status == 429 else None)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        try:
            resp = await client.request(request.method, "/" + path, content=body, headers=headers)
        except httpx.HTTPError:
            perimeter.stop("gateway", "edge unavailable", 502)
            return JSONResponse({"error": "edge unavailable", "layer": "gateway"}, status_code=502)
        return Response(content=resp.content, status_code=resp.status_code,
                        headers={k: resp.headers[k] for k in PASS_BACK if k in resp.headers})

    return app
