"""The bank's customers, from $RF_DATA/bank/accounts.csv (prd/03 section 4.2; built by `python -m vault build`).

An account that isn't in the file is unknown to the bank: rule R0 holds it, and the bounded fallback holds it too. Lab
bots use `lab-` accounts, which are never in the file (CONTRACTS section 0).
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Optional

log = logging.getLogger("bank.registry")


class Registry:
    def __init__(self, segments: dict, source: Optional[str] = None):
        self.segments = segments
        self.source = source

    @classmethod
    def load(cls, data_dir: str) -> "Registry":
        path = os.path.join(data_dir, "bank", "accounts.csv")
        if not os.path.exists(path):
            log.warning("%s is missing: every account is unknown (run `python -m vault build`)", path)
            return cls({})
        with open(path, newline="", encoding="utf-8") as fh:
            segments = {row["acct"]: row["segment"] for row in csv.DictReader(fh)}
        return cls(segments, path)

    def segment(self, acct: str) -> Optional[str]:
        return self.segments.get(acct)

    def __len__(self) -> int:
        return len(self.segments)
