"""python -m lab run --scenario S3 --run-id s3-shield-01 --system shield --base-rps 200

Sends to 127.0.0.1:8080 (plus RF_PORT_OFFSET) only. Prints a JSON summary.
Exit codes: 0 valid run, 2 achieved under 95% of offered (invalid run), 3 refused (rate cap or bad input).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import config
from .engine import Lab
from .scenario import load

SYSTEMS = ("bank_only", "bank_realistic", "shield", "standalone", "submitted_design")


def main(argv=None) -> int:
    data = os.environ.get("RF_DATA", "/srv/riskfabric/data")
    ap = argparse.ArgumentParser(prog="lab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one scenario")
    run.add_argument("--scenario", required=True, help="S1, S2, S3, S4, S4b, S5 or a path to a scenario JSON")
    run.add_argument("--run-id", required=True)
    run.add_argument("--system", required=True, choices=SYSTEMS)
    run.add_argument("--base-rps", type=float, required=True, help="the base legitimate rate R (set by C at H10)")
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--duration-s", type=float, default=None)
    run.add_argument("--replay", default=os.path.join(data, "replay", "hi_small_test.csv"))
    run.add_argument("--truth-dir", default=os.path.join(data, "truth"))
    run.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    args = ap.parse_args(argv)

    try:
        sc = load(args.scenario, args.base_rps, config.lab_rate(), args.seed, args.duration_s)
    except (OSError, ValueError, KeyError) as exc:
        print(f"lab: cannot load scenario: {exc}", file=sys.stderr)
        return 3
    plan = {"scenario": sc.id, "target": config.base_url(), "duration_s": sc.duration_s, "seed": sc.seed,
            "offered_requests": round(sc.offered(), 1), "peak_rps": round(sc.peak_rps(), 1), "cap_rps": config.MAX_TOTAL_RPS,
            "populations": [{"kind": p.kind, "rps": round(p.rps, 2), "start_s": p.start_s, "stop_s": p.stop_s, **p.options}
                            for p in sc.pops], "faults": sc.faults}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    truth = os.path.join(args.truth_dir, f"{args.run_id}.jsonl")
    try:
        summary = asyncio.run(Lab(sc, args.run_id, args.system, args.replay, truth).run())
    except ValueError as exc:
        print(f"lab: refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(summary))
    return 0 if summary["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
