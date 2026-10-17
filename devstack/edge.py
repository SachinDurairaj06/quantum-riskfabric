"""Laptop stand-in for D's HAProxy edge (prd/06 D-06, D-07, D-08 and D-25), for machines without HAProxy.

It listens where the real edge does (8080 plus RF_PORT_OFFSET) and keeps the same contract: the CONTRACTS section 3
headers, the per-IP limit, the cluster actions, the routing and bypass, and one `edge.log` entry per request with
every field. Three modes match the three configs:
- `riskfabric` (`haproxy.cfg`): cluster actions from Sentinel, then the decision service, 8001 primary and 8002 standby,
  with bypass to the bank channel when neither is healthy;
- `statusquo` (`haproxy.statusquo.cfg`): the per-IP limit only, straight to the bank channel;
- `realistic` (`haproxy.realistic.cfg`): as status quo, plus 429 while a fingerprint's rate over the last second is
  above `cfg:fp_limit_rps`.

Differences from the real edge, on purpose: it applies Sentinel's latest `threat` entry itself every 0.5 s (no
applier, no maps), and throttle lets a cluster through at THROTTLE_RPS per second with 429 above that. It is not a
performance reference: the LinuxONE VM runs HAProxy.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("devstack.edge")

MODES = ("riskfabric", "statusquo", "realistic")
IP_LIMIT_RPS = 20.0      # stand-in value; D sets the real per-IP limit in the HAProxy configs
THROTTLE_RPS = 5.0       # per cluster while its action is throttle
HEALTH_S, THREAT_S, CFG_S, FLUSH_S = 1.0, 0.5, 1.0, 0.02
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}
# headers only the edge and the router may set; a client that sends them has them removed
INTERNAL = {"x-client-ip", "x-cluster", "x-cluster-risk", "x-edge-action", "x-deadline-ms", "x-edge-ts"}
BANK_PATHS = {"/pay": "/bank/pay", "/verify": "/bank/verify"}


class Window:
    """Requests per key in the current one-second window."""

    def __init__(self):
        self.second, self.counts = 0, {}

    def hit(self, key: str, now: float) -> int:
        if int(now) != self.second:
            self.second, self.counts = int(now), {}
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class Sliding:
    """Each key's request times over the last second, counting denied requests too, as a stick table does."""

    def __init__(self):
        self.times: dict = {}

    def rate(self, key: str, now: float) -> int:
        q = self.times.setdefault(key, collections.deque())
        q.append(now)
        while q and q[0] <= now - 1.0:
            q.popleft()
        return len(q)


class ClusterMap:
    """Sentinel's latest desired state: key -> (cluster id, action, until). A cluster missing from it is released."""

    def __init__(self):
        self.by_key: dict = {}

    def load(self, clusters: list) -> None:
        table = {}
        for c in clusters:
            if c.get("action") in ("throttle", "stepup", "block") and c.get("key"):
                table[f"{c.get('key_type', 'fp')}:{c['key']}"] = (c.get("id", ""), c["action"], float(c.get("until") or 0))
        self.by_key = table

    def match(self, fp: str, asn: str, now: float) -> Optional[tuple]:
        for key in (f"fp_asn:{fp}|{asn}", f"fp:{fp}"):
            hit = self.by_key.get(key)
            if hit and (not hit[2] or hit[2] >= now):
                return hit[0], hit[1]
        return None


class Edge:
    def __init__(self, mode: str, offset: int = 0, deadline_ms: int = 100):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode, self.deadline_ms = mode, deadline_ms
        self.decision = [f"http://127.0.0.1:{8001 + offset}", f"http://127.0.0.1:{8002 + offset}"]
        self.bank = f"http://127.0.0.1:{9000 + offset}"
        self.healthy = {url: True for url in self.decision}
        self.fails = {url: 0 for url in self.decision}
        self.per_ip, self.per_cluster, self.per_fp = Window(), Window(), Sliding()
        self.clusters = ClusterMap()
        self.fp_limit: Optional[float] = None
        self.log_buffer: list = []

    def backends(self) -> list:
        """Candidate (url, path map) in order. Bypass to the bank's own channel when no decision replica is up."""
        if self.mode != "riskfabric":
            return [(self.bank, BANK_PATHS)]
        up = [(u, None) for u in self.decision if self.healthy[u]]
        return up + [(self.bank, BANK_PATHS)]

    def gate(self, ip: str, fp: str, asn: str, now: float) -> tuple:
        """(status or None to pass, cluster id, edge_action)."""
        if self.per_ip.hit(ip, now) > IP_LIMIT_RPS:
            return 429, "", "throttle"
        if self.mode == "realistic" and self.fp_limit is not None and self.per_fp.rate(fp, now) > self.fp_limit:
            return 429, "", "throttle"
        if self.mode == "riskfabric":
            hit = self.clusters.match(fp, asn, now)
            if hit:
                cid, action = hit
                if action == "block":
                    return 403, cid, "block"
                if action == "throttle" and self.per_cluster.hit(cid, now) > THROTTLE_RPS:
                    return 429, cid, "throttle"
                return None, cid, action
        return None, "", "pass"

    def record(self, **fields) -> None:
        self.log_buffer.append({k: str(v) for k, v in fields.items()})


async def _health_forever(edge: Edge, client: httpx.AsyncClient) -> None:
    while True:
        for url in edge.decision:
            try:
                ok = (await client.get(f"{url}/health", timeout=0.5)).status_code == 200
            except httpx.HTTPError:
                ok = False
            edge.fails[url] = 0 if ok else edge.fails[url] + 1
            edge.healthy[url] = ok or edge.fails[url] < 2      # down after 2 failed checks, up after 1 good one
        await asyncio.sleep(HEALTH_S)


async def _threat_forever(edge: Edge, r) -> None:
    while True:
        try:
            latest = await r.xrevrange("threat", count=1)
            edge.clusters.load(json.loads(latest[0][1].get("clusters") or "[]") if latest else [])
        except Exception:
            log.exception("could not read threat")
        await asyncio.sleep(THREAT_S)


async def _cfg_forever(edge: Edge, r) -> None:
    while True:
        try:
            fp_limit, deadline = await r.mget("cfg:fp_limit_rps", "cfg:deadline_ms")
            edge.fp_limit = float(fp_limit) if fp_limit else None
            if deadline:
                edge.deadline_ms = int(float(deadline))
        except Exception:
            log.exception("could not read cfg")
        await asyncio.sleep(CFG_S)


async def _flush_forever(edge: Edge, r) -> None:
    while True:
        await asyncio.sleep(FLUSH_S)
        batch, edge.log_buffer = edge.log_buffer, []
        if batch:
            try:
                pipe = r.pipeline(transaction=False)
                for fields in batch:
                    pipe.xadd("edge.log", fields, maxlen=100_000, approximate=True)
                await pipe.execute()
            except Exception:
                log.exception("edge.log write failed")


def create_app(mode: str, redis=None, offset: int = 0, transport=None, background: bool = True) -> FastAPI:
    edge = Edge(mode, offset, int(os.environ.get("RF_DEADLINE_MS", "100")))
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(3.0),
                               limits=httpx.Limits(max_connections=500, max_keepalive_connections=200))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        tasks = []
        if background and redis is not None:
            tasks += [_threat_forever(edge, redis), _cfg_forever(edge, redis), _flush_forever(edge, redis)]
        if background and mode == "riskfabric":
            tasks.append(_health_forever(edge, client))
        running = [asyncio.create_task(t) for t in tasks]
        try:
            yield
        finally:
            for t in running:
                t.cancel()
            await client.aclose()

    app = FastAPI(title="Edge stand-in", lifespan=lifespan)
    app.state.edge = edge

    @app.post("/{path:path}")
    async def handle(path: str, request: Request):
        t0, now = time.perf_counter(), time.time()
        path = "/" + path
        h = request.headers
        req_id = h.get("x-request-id") or str(uuid.uuid4())
        ip = (h.get("x-forwarded-for") or "").split(",")[0].strip()
        fp, asn = h.get("x-client-fp", ""), h.get("x-client-asn", "")
        status, cluster, action = (404, "", "pass") if path not in BANK_PATHS else edge.gate(ip, fp, asn, now)
        backend, resp = "edge", None
        if status is None:
            body = await request.body()
            fwd = {k.lower(): v for k, v in h.items() if k.lower() not in HOP | INTERNAL}
            fwd.update({"x-request-id": req_id, "x-client-ip": ip, "x-deadline-ms": str(edge.deadline_ms),
                        "x-edge-ts": f"{now:.6f}"})
            if cluster:
                fwd["x-cluster"] = cluster
            if action == "stepup":
                fwd["x-edge-action"] = "stepup"
            for url, paths in edge.backends():
                try:
                    resp = await client.post(url + (paths[path] if paths else path), content=body, headers=fwd)
                    backend = url.rsplit(":", 1)[-1]
                    break
                except httpx.HTTPError:
                    if url in edge.healthy:
                        edge.healthy[url] = False   # redispatch to the next candidate, as HAProxy does
            status = resp.status_code if resp is not None else 503
        edge.record(ts=f"{now:.6f}", req_id=req_id, ip=ip, fp=fp, asn=asn, method="POST", path=path, status=status,
                    ms=f"{(time.perf_counter() - t0) * 1000:.3f}", backend=backend, cluster=cluster,
                    edge_action=action, known="1" if h.get("x-client-known") == "1" else "0")
        if resp is not None:
            return Response(content=resp.content, status_code=resp.status_code,
                            headers={k: resp.headers[k] for k in ("content-type", "retry-after") if k in resp.headers})
        if status == 429:
            return JSONResponse({"error": "throttled"}, status_code=429, headers={"Retry-After": "1"})
        return JSONResponse({"error": {403: "blocked", 404: "not found"}.get(status, "no backend")}, status_code=status)

    return app
