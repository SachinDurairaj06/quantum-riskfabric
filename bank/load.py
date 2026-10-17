"""`python -m bank load`: opening balances into `bank:bal:{acct}` and tiers into `tier:{acct}` (prd/03 4.2, A-08).

It reads $RF_DATA/bank/accounts.csv and tiers.csv, which `python -m vault build` writes. D's `deploy/reset.sh` runs it
before every run, after clearing the old keys, so every run starts from the same balances.
"""
from __future__ import annotations

import csv
import os

BATCH = 5000


def _rows(path: str):
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def load(r, data_dir: str) -> dict:
    """r is a synchronous redis client. Returns how many keys of each kind were written."""
    counts = {"balances": 0, "tiers": 0}
    for name, kind, key, field in (("accounts.csv", "balances", "bank:bal:{}", "opening_balance"),
                                   ("tiers.csv", "tiers", "tier:{}", "tier")):
        pipe = r.pipeline(transaction=False)
        for row in _rows(os.path.join(data_dir, "bank", name)):
            pipe.set(key.format(row["acct"]), row[field])
            counts[kind] += 1
            if counts[kind] % BATCH == 0:
                pipe.execute()
                pipe = r.pipeline(transaction=False)
        pipe.execute()
    return counts
