"""Reading the replay file (CONTRACTS section 10): header txn_id,ts,from_acct,to_acct,amount,currency,format,label."""
from __future__ import annotations

import csv
import itertools
from typing import Iterator

HEADER = ["txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format", "label"]


def _row(raw: dict) -> dict:
    return {"txn_id": raw["txn_id"], "ts": float(raw["ts"]), "from_acct": raw["from_acct"], "to_acct": raw["to_acct"],
            "amount": float(raw["amount"]), "currency": raw["currency"], "format": raw["format"], "label": int(raw["label"])}


def rows(path: str) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != HEADER:
            raise ValueError(f"{path}: header {reader.fieldnames} is not {HEADER}")
        for raw in reader:
            yield _row(raw)


def first_rows(path: str, n: int) -> list:
    return list(itertools.islice(rows(path), n))


def laundering_from_end(path: str, n: int) -> list:
    """Label-1 rows taken from the end of the file, so the main replay of a short run never reaches them."""
    found = [r for r in rows(path) if r["label"] == 1]
    return list(reversed(found[-n:])) if n > 0 else []


def accounts(path: str, n: int, limit_rows: int = 20000) -> list:
    """A small, stable group of real sender accounts (used by card testers)."""
    seen: list = []
    for r in itertools.islice(rows(path), limit_rows):
        if r["label"] == 0 and r["from_acct"] not in seen:
            seen.append(r["from_acct"])
            if len(seen) >= n:
                break
    return seen
