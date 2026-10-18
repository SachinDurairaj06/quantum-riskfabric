"""Read a transaction source into one time-ordered, normalised table with a 60/20/20 time split (prd/03 section 2).

Sources:
- `hi_small`: IBM's `HI-Small_Trans.csv` (the AML dataset), used on the VM;
- `cards`: the vault's sealed card file (`transactions_history.csv`), via the same parsing the bank registry uses, so
  account names match (`card-<User>-<Card>`, `merchant-<Merchant Name>`);
- `replay`: any CSV in the replay format `txn_id,ts,from_acct,to_acct,amount,currency,format,label`.

Every reader returns the columns `txn_id, ts, from_acct, to_acct, amount, label, day`, sorted by `ts`. `split` is added
by time at day boundaries: the earliest 60% of payments are `train`, the next 20% `val`, the last 20% `test`. Labels
are used only to train and to score recall; they are never features (CONTRACTS section 10, prd/03 section 2).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SPLIT = (0.6, 0.8)
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
REPLAY_COLS = ["txn_id", "ts", "from_acct", "to_acct", "amount", "label"]


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["amount"] > 0].copy()
    df["label"] = df["label"].astype(int)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    if "txn_id" not in df.columns:
        df["txn_id"] = [f"t-{i + 1:09d}" for i in range(len(df))]
    df["day"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return df[["txn_id", "ts", "from_acct", "to_acct", "amount", "label", "day"]]


def read_hi_small(path: str) -> pd.DataFrame:
    cols = ["Timestamp", "From Bank", "Account", "To Bank", "Account.1", "Amount Paid", "Is Laundering"]
    df = pd.read_csv(path, usecols=cols, dtype={"From Bank": str, "Account": str, "To Bank": str, "Account.1": str})
    ts = pd.to_datetime(df["Timestamp"], utc=True, errors="coerce")
    out = pd.DataFrame({
        "ts": ((ts - EPOCH) // pd.Timedelta(seconds=1)).astype("float64"),
        "from_acct": df["From Bank"].str.strip() + "-" + df["Account"].str.strip(),
        "to_acct": df["To Bank"].str.strip() + "-" + df["Account.1"].str.strip(),
        "amount": pd.to_numeric(df["Amount Paid"], errors="coerce"),
        "label": pd.to_numeric(df["Is Laundering"], errors="coerce").fillna(0),
    })
    return _finish(out[out["ts"].notna() & out["amount"].notna()])


def read_cards(path: str) -> pd.DataFrame:
    from vault.bankdata import card_frame  # A-owned, same parsing as the bank registry

    parts = [card_frame(c)[["acct", "payee", "ts", "amount", "label"]]
             for c in pd.read_csv(path, dtype=str, chunksize=250_000, keep_default_na=False,
                                  usecols=["User", "Card", "Year", "Month", "Day", "Time", "Amount", "Use Chip",
                                           "Merchant Name", "Is Fraud?"])]
    df = pd.concat(parts, ignore_index=True).rename(columns={"acct": "from_acct", "payee": "to_acct"})
    df["ts"] = df["ts"].astype("float64")
    return _finish(df)


def read_replay(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in set(REPLAY_COLS))
    df["ts"] = df["ts"].astype("float64")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return _finish(df[df["amount"].notna()])


READERS = {"hi_small": read_hi_small, "cards": read_cards, "replay": read_replay}


def read(source: str, path: str) -> pd.DataFrame:
    if source not in READERS:
        raise ValueError(f"source must be one of {', '.join(READERS)}")
    return READERS[source](path)


def add_split(df: pd.DataFrame) -> pd.DataFrame:
    """Add a `split` column (train/val/test) by time, cut at day boundaries so a day never straddles two splits."""
    per_day = df.groupby("day", sort=True).size()
    cum = (per_day.cumsum() / len(df)).to_numpy()
    days = per_day.index.to_numpy()
    last_train = days[min(np.searchsorted(cum, SPLIT[0]), len(days) - 1)]
    last_val = days[min(np.searchsorted(cum, SPLIT[1]), len(days) - 1)]
    split = np.where(df["day"].to_numpy() <= last_train, "train",
                     np.where(df["day"].to_numpy() <= last_val, "val", "test"))
    return df.assign(split=split)
