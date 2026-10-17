"""SYNTHETIC look-alike files with the IBM sample headers, for tests and dry runs only. They are NOT IBM data.

The real files come with the Jupyter Lab environment on the LinuxONE VM; copy those into $RF_DATA/vault/ for real runs.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import random

CARD_HEADER = ["User", "Card", "Year", "Month", "Day", "Time", "Amount", "Use Chip", "Merchant Name", "Merchant City",
               "Merchant State", "Zip", "MCC", "Errors?", "Is Fraud?"]
CHIP = ["Swipe Transaction", "Chip Transaction", "Online Transaction"]


def write(vroot: str, rows: int = 3000, users: int = 30, cards: int = 2, clients: int = 40, fraud_users=(3, 7),
          seed: int = 7) -> dict:
    """Writes the five sample kinds into `vroot`. Card rows run in time order from 2015 to 2019."""
    rng = random.Random(seed)
    os.makedirs(os.path.join(vroot, "sample_data", "w2"), exist_ok=True)
    start = dt.datetime(2015, 1, 1)
    merchants = [str(rng.randrange(-9 * 10 ** 17, 9 * 10 ** 17)) for _ in range(60)]
    pairs = set()
    with open(os.path.join(vroot, "transactions_history.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CARD_HEADER)
        for i in range(rows):
            when = start + dt.timedelta(days=i * 1825 // rows)
            user, card = rng.randrange(users), rng.randrange(cards)
            pairs.add((user, card))
            amount = rng.uniform(2, 300) if rng.random() < 0.9 else rng.uniform(300, 4000)
            if rng.random() < 0.02:
                amount = -amount  # a refund
            fraud = user in fraud_users and rng.random() < 0.25
            w.writerow([user, card, when.year, when.month, when.day, f"{rng.randrange(24):02d}:{rng.randrange(60):02d}",
                        f"${amount:.2f}", rng.choice(CHIP), rng.choice(merchants), "Springfield", "IL", "62701", "5411",
                        "", "Yes" if fraud else "No"])
    with open(os.path.join(vroot, "clients.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CONT_ID", "ACTIVITY_LEVEL", "AGE_YEARS", "ANNUAL_INCOME", "ANNUAL_INVEST", "CHURN"])
        for i in range(clients):
            w.writerow([1000 + i, rng.randrange(1, 6), rng.randrange(20, 80), rng.randrange(20000, 200000),
                        rng.randrange(0, 50000), rng.randrange(2)])
    with open(os.path.join(vroot, "transactions.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CONT_ID", "TX_TOTAL_AMT", "DATE_CREDIT"])
        for i in range(clients * 5):
            day = start + dt.timedelta(days=rng.randrange(1825))
            w.writerow([1000 + rng.randrange(clients), f"{rng.uniform(10, 5000):.2f}", day.strftime("%Y-%m-%d")])
    with open(os.path.join(vroot, "sample_data", "sample_loan.csv"), "w", newline="", encoding="utf-8") as fh:
        fh.write("applicant_id,loan_amount,term_months\n1,25000,60\n2,8000,36\n")
    with open(os.path.join(vroot, "sample_data", "w2", "1.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n% synthetic look-alike, not an IBM document\n")
    return {"card_rows": rows, "card_accounts": len(pairs), "clients": clients}
