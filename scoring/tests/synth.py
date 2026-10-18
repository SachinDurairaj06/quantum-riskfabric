"""SYNTHETIC transactions in the replay format, for scoring tests only. NOT IBM data, never a measurement.

Three populations so the model's top-2% candidates look like real ones — a mix of fraud and hard false positives,
not perfectly separable:
- normal customers: small payments to random merchants (label 0);
- businesses: legitimate fan-outs to many fresh payees, larger amounts (label 0) — they look like laundering;
- mules: laundering fan-outs to fresh drops, same shape (label 1).
Businesses and mules share the fan-out / scatter / fresh-payee signal, so the gate's candidate pool has both classes.
"""
from __future__ import annotations

import csv
import random

START = 1_600_000_000       # a fixed epoch start
DAY = 86_400


def _fanout(rows, n, src, t0, size, amt, label, rng, prefix):
    for j in range(size):
        n[0] += 1
        rows.append([f"t-{n[0]:09d}", t0 + j * 20, src, f"{prefix}-{rng.randrange(5000):04d}",
                     round(rng.uniform(*amt), 2), "USD", "online", label])


def write(path: str, days: int = 60, per_day: int = 150, bursts_per_day: int = 6, legit_bursts_per_day: int = 6,
          burst_size: int = 15, customers: int = 300, merchants: int = 120, mules: int = 20, seed: int = 11) -> dict:
    rng = random.Random(seed)
    mule_ids = [f"mule-{i:03d}" for i in range(mules)]      # persistent, so fan-out history grows over time
    biz_ids = [f"biz-{i:03d}" for i in range(mules)]
    rows, n, fraud = [], [0], 0
    for d in range(days):
        base = START + d * DAY
        for _ in range(per_day):
            n[0] += 1
            rows.append([f"t-{n[0]:09d}", base + rng.randrange(DAY), f"cust-{rng.randrange(customers):04d}",
                         f"merch-{rng.randrange(merchants):04d}", round(rng.gauss(50, 20), 2) + 60, "USD", "chip", 0])
        # businesses and mules behave identically; only the label differs, so the model can't separate them
        # cleanly and the top-2% candidate pool holds both classes, as a real analyst queue does
        for _ in range(legit_bursts_per_day):
            _fanout(rows, n, rng.choice(biz_ids), base + rng.randrange(DAY - 3600), burst_size, (800, 3000), 0, rng, "pay")
        for _ in range(bursts_per_day):
            _fanout(rows, n, rng.choice(mule_ids), base + rng.randrange(DAY - 3600), burst_size, (800, 3000), 1, rng, "pay")
            fraud += burst_size
    rng.shuffle(rows)                       # unsorted on disk; the reader sorts by ts
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format", "label"])
        w.writerows(rows)
    return {"rows": len(rows), "fraud": fraud, "days": days}
