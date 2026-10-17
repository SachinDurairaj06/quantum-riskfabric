"""python -m vault <seal|verify|build|status> [--data DIR]

  seal     record every file's SHA-256, size, header and rows in MANIFEST.json (--reseal for a deliberate change)
  verify   re-hash everything; exit 1 if any file changed, went missing or was added
  build    from sealed, unchanged files only: bank/accounts.csv, bank/tiers.csv and replay/ibm_cards_test.csv
  status   what the vault holds, by kind

The data folder is $RF_DATA (default /srv/riskfabric/data); the vault is its `vault/` subfolder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import bankdata, store
from .store import VaultError


def _status(data_dir: str) -> dict:
    manifest = store.load_manifest(data_dir)
    kinds: dict = {}
    for e in manifest["files"]:
        k = kinds.setdefault(e["kind"], {"files": 0, "rows": 0, "bytes": 0})
        k["files"] += 1
        k["rows"] += e.get("rows", 0)
        k["bytes"] += e["bytes"]
    problems = [{"file": e["file"], "expected": e["expected_kind"], "missing": e["missing_columns"]}
                for e in manifest["files"] if e.get("missing_columns")]
    return {"digest": manifest["digest"], "kinds": kinds, "header_problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vault")
    ap.add_argument("cmd", choices=("seal", "verify", "build", "status"))
    ap.add_argument("--data", default=os.environ.get("RF_DATA", "/srv/riskfabric/data"))
    ap.add_argument("--reseal", action="store_true", help="seal: replace an existing manifest on purpose")
    ap.add_argument("--replay-rows", type=int, default=bankdata.REPLAY_ROWS)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "seal":
            m = store.seal(args.data, reseal=args.reseal)
            print(json.dumps({"sealed": len(m["files"]), "digest": m["digest"], **_status(args.data)}, indent=1))
        elif args.cmd == "verify":
            result = store.verify(args.data)
            print(json.dumps(result, indent=1))
            return 0 if result["ok"] else 1
        elif args.cmd == "build":
            print(json.dumps(bankdata.build(args.data, args.replay_rows), indent=1))
        else:
            print(json.dumps(_status(args.data), indent=1))
    except VaultError as exc:
        print(f"vault: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
