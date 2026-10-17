"""`python -m perimeter limit`: the perimeter's global ceiling, from the capacity measured on this host.

    ceiling = 0.8 x cores x 1,000,000 / (edge + Sentinel + decision + bank CPU microseconds per request)

The CPU figures and the core count come from C's capacity run (`python -m eval.scenarios.runner capacity`), saved as
eval/results/CAP__shield__<stamp>.json. Every request is costed as if it passed the edge, so the ceiling still holds
on a day with no attack to shed. The 0.8 leaves 20% headroom for the OS, Valkey and bursts. Run it on the LinuxONE VM
after the capacity run there: a laptop's figures say nothing about IBM Z. Without a capacity run there is no
ceiling, because this command never guesses a number.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import sys
from typing import Optional

SAFETY = 0.8
TIERS = ("edge", "sentinel", "decision", "bank")
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(os.path.dirname(HERE), "eval", "results")


def ceiling(cap: dict, safety: float = SAFETY) -> dict:
    cpu = cap.get("cpu_us_per_req") or {}
    missing = [t for t in TIERS if not cpu.get(t)]
    if not cap.get("cores") or missing:
        raise ValueError(f"the capacity result lacks {'cores' if not cap.get('cores') else ', '.join(missing)}: "
                         "re-run `python -m eval.scenarios.runner capacity` on this host")
    per_request = sum(float(cpu[t]) for t in TIERS)
    return {"ceiling_rps": int(safety * cap["cores"] * 1e6 / per_request), "cores": cap["cores"],
            "per_request_us": round(per_request, 2), "cpu_us_per_req": {t: cpu[t] for t in TIERS}, "safety": safety,
            "measured_on": cap.get("host"), "command": cap.get("command")}


def latest_capacity(results_dir: str = RESULTS) -> Optional[dict]:
    files = sorted(glob.glob(os.path.join(results_dir, "CAP__*.json")))
    for path in reversed(files):
        with open(path, encoding="utf-8") as fh:
            run = json.load(fh)
        if run.get("valid") and run.get("capacity"):
            return dict(run["capacity"], host=run["capacity"].get("host") or run.get("host"), file=path)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="perimeter limit")
    ap.add_argument("--set", action="store_true", help="write cfg:perimeter_limit_rps to Valkey")
    ap.add_argument("--results", default=RESULTS)
    args = ap.parse_args(argv)
    cap = latest_capacity(args.results)
    if cap is None:
        print("perimeter limit: no valid capacity run in eval/results/. The ceiling stays off. Run "
              "`python -m eval.scenarios.runner capacity --base-rps R` on the LinuxONE VM first.", file=sys.stderr)
        return 1
    if cap.get("host") and cap["host"] != platform.node():
        print(f"perimeter limit: the capacity was measured on {cap['host']}, not on this host "
              f"({platform.node()}); refusing to apply another machine's numbers.", file=sys.stderr)
        return 1
    try:
        out = ceiling(cap)
    except ValueError as exc:
        print(f"perimeter limit: {exc}", file=sys.stderr)
        return 1
    out["from_file"] = os.path.relpath(cap["file"], os.path.dirname(HERE)).replace(os.sep, "/")
    if args.set:
        import redis

        redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0")).set(
            "cfg:perimeter_limit_rps", str(out["ceiling_rps"]))
        out["set"] = "cfg:perimeter_limit_rps"
    print(json.dumps(out, indent=1))
    return 0
