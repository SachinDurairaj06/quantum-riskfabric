"""Scenario files (prd/04 section 3.3): populations with rates relative to the base rate R, start and stop times.

Format of lab/scenarios/<S>.json:
{"id": "S3", "duration_s": 210, "warmup_s": 30, "seed": 13,
 "populations": [
   {"kind": "replay", "rate_x": 1.0, "start_s": 0, "stop_s": 210},
   {"kind": "bot", "rate_x": 4.0, "start_s": 60, "stop_s": 180, "identities": 2000, "tool_fps": 1, "networks": 3},
   {"kind": "cardtest", "rate_x": 0.05, "start_s": 60, "stop_s": 180, "burst_on_s": 5, "burst_off_s": 10},
   {"kind": "launder", "rate_rps": 2, "start_s": 60, "stop_s": 180, "identity": "attacker"}],
 "faults": [{"type": "lo_delay", "start_s": 60, "stop_s": 120, "delay_ms": 20}]}
`faults` are informational for C's runner; the lab never applies faults.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

KINDS = ("replay", "bot", "cardtest", "launder")
HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Pop:
    kind: str
    rps: float
    start_s: float
    stop_s: float
    options: dict = field(default_factory=dict)

    def active(self, t: float) -> bool:
        if not (self.start_s <= t < self.stop_s):
            return False
        on, off = self.options.get("burst_on_s"), self.options.get("burst_off_s")
        if on and off:
            return ((t - self.start_s) % (on + off)) < on
        return True

    def active_seconds(self, duration: float) -> float:
        stop = min(self.stop_s, duration)
        span = max(0.0, stop - self.start_s)
        on, off = self.options.get("burst_on_s"), self.options.get("burst_off_s")
        if on and off and span > 0:
            full, rest = divmod(span, on + off)
            return full * on + min(rest, on)
        return span


@dataclass
class Scenario:
    id: str
    duration_s: float
    warmup_s: float
    seed: int
    pops: list
    faults: list

    def offered(self) -> float:
        return sum(p.rps * p.active_seconds(self.duration_s) for p in self.pops)

    def peak_rps(self) -> float:
        times = sorted({0.0} | {p.start_s for p in self.pops} | {p.stop_s for p in self.pops})
        return max((sum(p.rps for p in self.pops if p.start_s <= t < p.stop_s) for t in times), default=0.0)


def path_for(scenario_id: str) -> str:
    return os.path.join(HERE, "scenarios", f"{scenario_id}.json")


def load(scenario_id_or_path: str, base_rps: float, lab_rate: float = 1.0, seed: Optional[int] = None,
         duration_s: Optional[float] = None) -> Scenario:
    path = scenario_id_or_path if scenario_id_or_path.endswith(".json") else path_for(scenario_id_or_path)
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    pops = []
    for p in spec["populations"]:
        kind = p["kind"]
        if kind not in KINDS:
            raise ValueError(f"{path}: unknown population kind {kind!r}")
        if "rate_rps" in p:
            rps = float(p["rate_rps"]) * lab_rate
        else:
            rps = float(p["rate_x"]) * base_rps * lab_rate
        opts = {k: v for k, v in p.items() if k not in ("kind", "rate_x", "rate_rps", "start_s", "stop_s")}
        pops.append(Pop(kind, rps, float(p.get("start_s", 0)), float(p.get("stop_s", spec["duration_s"])), opts))
    return Scenario(id=spec["id"], duration_s=float(duration_s or spec["duration_s"]), warmup_s=float(spec.get("warmup_s", 30)),
                    seed=int(seed if seed is not None else spec.get("seed", 1)), pops=pops, faults=spec.get("faults", []))
