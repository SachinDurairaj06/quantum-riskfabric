"""python -m scoring <train|export> --source <hi_small|cards|replay> --path FILE

  train   fit gfp-gbm-v1 and raw-gbm-v1, tune policy.json, write the models and scoring/report.json
  export  write $RF_DATA/features/qml_candidates.csv and qml_capacity.csv for the quantum gate (CONTRACTS 13)

Models go to $RF_DATA/models (--model-dir). The backend is sklearn here; on the VM set RF_MODEL_BACKEND=lightgbm or
snapml. A run on synthetic look-alike data only proves the pipeline; real metrics come from HI-Small on the VM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None) -> int:
    data = os.environ.get("RF_DATA", "/srv/riskfabric/data")
    ap = argparse.ArgumentParser(prog="scoring")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("train", "export"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True, choices=("hi_small", "cards", "replay"))
        p.add_argument("--path", required=True, help="the source file")
        p.add_argument("--model-dir", default=os.path.join(data, "models"))
        if name == "export":
            p.add_argument("--out-dir", default=os.path.join(data, "features"))
    args = ap.parse_args(argv)

    from . import pipeline

    if args.cmd == "train":
        report = pipeline.train(args.source, args.path, args.model_dir)
        print(json.dumps(report, indent=1))
        if report["synthetic"]:
            print("NOTE: synthetic look-alike data — these numbers only check that the pipeline runs, not accuracy.",
                  file=sys.stderr)
    else:
        print(json.dumps(pipeline.export(args.source, args.path, args.out_dir, args.model_dir), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
