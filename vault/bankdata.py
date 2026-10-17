"""`python -m vault build`: the bank's files from the sealed vault (prd/03 section 4.2), plus a replay for the lab.

Outputs, all under $RF_DATA and never in git:
- `bank/accounts.csv`: `acct,bank_id,segment,opening_balance,first_seen_ts` (CONTRACTS section 11);
- `bank/tiers.csv`: `acct,tier`, which `python -m bank load` writes to `tier:{acct}` (CONTRACTS section 6);
- `replay/ibm_cards_test.csv`: `txn_id,ts,from_acct,to_acct,amount,currency,format,label` (CONTRACTS section 10);
- `vault/BUILD.json`: which sealed files were used, the split and the counts.

The rules, applied to whatever the vault holds:
- **Card transactions** (the fraud notebook's file). One account per user and card, `card-<User>-<Card>`; payees are
  `merchant-<Merchant Name>`. Time is Year, Month, Day and Time as UTC. Split 60/20/20 by time at day boundaries
  (prd/03 section 2). If both the full history and the notebook's 40k test sample are present, only the larger file
  is read, so no payment counts twice.
- **Clients** and their transactions (the churn notebook's files). One account per client, `client-<CONT_ID>`.
- **Segment:** `business` if the account's mean monthly volume is above the 90th percentile of its group, otherwise
  `retail`. For cards only the train period counts.
- **Opening balance:** the larger of 1,000 and 3 times the account's highest daily volume (cards: train period).
- **Tiers,** until A's model exists (A-08 uses the model's mean score as well): the share of an account's train and
  validation card payments labelled fraud. The top 2% of card accounts are `high`, the next 8% `medium`, the rest
  `low`; an account with no fraud history is always `low`. Clients have no fraud labels, so they are `low`.
- **Replay:** the earliest `--replay-rows` card payments of the test period with an amount above 0, in time order,
  with `txn_id` values below t-900000000. Refunds (amounts of 0 or less) are left out.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np
import pandas as pd

from . import store
from .store import VaultError

CARD_COLS = ["User", "Card", "Year", "Month", "Day", "Time", "Amount", "Use Chip", "Merchant Name", "Is Fraud?"]
CHUNK_ROWS = 250_000
SPLIT = (0.6, 0.8)
MIN_BALANCE = 1000.0
BALANCE_DAYS = 3.0
BUSINESS_PCTL = 0.90
HIGH_TOP, MEDIUM_NEXT = 0.02, 0.08
REPLAY_ROWS = 500_000
REPLAY_NAME = "ibm_cards_test.csv"
DAY_S = 86400
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


class _Codes:
    """Account strings to small integers, so the daily table stays compact on large files."""

    def __init__(self):
        self.index: dict = {}
        self.names: list = []

    def encode(self, s: pd.Series) -> np.ndarray:
        for name in s.unique():
            if name not in self.index:
                self.index[name] = len(self.names)
                self.names.append(name)
        return s.map(self.index).to_numpy(dtype="int64")


def _card_chunks(path: str):
    yield from pd.read_csv(path, usecols=CARD_COLS, dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False)


def card_frame(c: pd.DataFrame) -> pd.DataFrame:
    """One chunk of the card file as account, payee, epoch seconds, amount, format and label."""
    hm = c["Time"].str.strip().str.split(":", n=1, expand=True)
    when = pd.to_datetime(pd.DataFrame({"year": c["Year"].astype(int), "month": c["Month"].astype(int),
                                        "day": c["Day"].astype(int), "hour": hm[0].astype(int),
                                        "minute": hm[1].str.slice(0, 2).astype(int)}), utc=True)
    return pd.DataFrame({
        "acct": "card-" + c["User"].str.strip() + "-" + c["Card"].str.strip(),
        "payee": "merchant-" + c["Merchant Name"].str.strip(),
        "ts": ((when - EPOCH) // pd.Timedelta(seconds=1)).astype("int64"),
        "amount": c["Amount"].str.replace(r"[$,\s]", "", regex=True).astype(float),
        "fmt": c["Use Chip"].str.strip().str.split(" ", n=1).str[0].str.lower(),
        "label": (c["Is Fraud?"].str.strip() == "Yes").astype("int64"),
    })


def _card_daily(path: str, codes: _Codes) -> pd.DataFrame:
    """Per account and day: payments, fraud labels, outflow and the first timestamp."""
    parts = []
    for chunk in _card_chunks(path):
        f = card_frame(chunk)
        g = pd.DataFrame({"a": codes.encode(f["acct"]), "day": f["ts"] // DAY_S, "n": 1, "fraud": f["label"],
                          "out": f["amount"].clip(lower=0), "first": f["ts"]})
        parts.append(g.groupby(["a", "day"], sort=False).agg(
            n=("n", "sum"), fraud=("fraud", "sum"), out=("out", "sum"), first=("first", "min")))
    if not parts:
        raise VaultError(f"{path} has no rows")
    return pd.concat(parts).groupby(level=["a", "day"]).agg(
        n=("n", "sum"), fraud=("fraud", "sum"), out=("out", "sum"), first=("first", "min")).reset_index()


def _split_days(daily: pd.DataFrame) -> tuple:
    """Last day of train and last day of validation: 60% and 80% of payments, cut at day boundaries."""
    per_day = daily.groupby("day")["n"].sum().sort_index()
    cum = (per_day.cumsum() / per_day.sum()).to_numpy()
    days = per_day.index.to_numpy()
    return int(days[np.searchsorted(cum, SPLIT[0])]), int(days[np.searchsorted(cum, SPLIT[1])])


def _segments(monthly: pd.Series) -> pd.Series:
    cut = monthly.dropna().quantile(BUSINESS_PCTL) if monthly.notna().any() else math.inf
    return np.where(monthly.fillna(-1.0) > cut, "business", "retail")


def _card_accounts(daily: pd.DataFrame, codes: _Codes, last_train: int, last_val: int) -> pd.DataFrame:
    n = len(codes.names)
    train = daily[daily["day"] <= last_train]
    month = pd.to_datetime(train["day"] * DAY_S, unit="s", utc=True)
    train = train.assign(month=month.dt.year * 12 + month.dt.month)
    seen = daily[daily["day"] <= last_val].groupby("a")[["n", "fraud"]].sum().reindex(range(n), fill_value=0)
    df = pd.DataFrame({
        "acct": codes.names,
        "monthly": train.groupby(["a", "month"])["out"].sum().groupby(level="a").mean().reindex(range(n)).to_numpy(),
        "peak": train.groupby("a")["out"].max().reindex(range(n)).fillna(0.0).to_numpy(),
        "first": daily.groupby("a")["first"].min().reindex(range(n)).to_numpy(),
        "seen": seen["n"].to_numpy(), "fraud": seen["fraud"].to_numpy(),
    })
    df["segment"] = _segments(df["monthly"])
    df["bank_id"] = "card"
    df["tier"] = _tiers(df)
    return df


def _tiers(df: pd.DataFrame) -> np.ndarray:
    share = np.where(df["seen"] > 0, df["fraud"] / df["seen"].clip(lower=1), 0.0)
    n_high = math.ceil(HIGH_TOP * len(df))
    n_risky = math.ceil((HIGH_TOP + MEDIUM_NEXT) * len(df))
    ranked = df.assign(share=share)[share > 0].sort_values(["share", "fraud", "acct"], ascending=[False, False, True])
    tier = pd.Series("low", index=df.index)
    tier[ranked.index[:n_high]] = "high"
    tier[ranked.index[n_high:n_risky]] = "medium"
    return tier.to_numpy()


def _ids(s: pd.Series) -> pd.Series:
    return s.str.strip().str.replace(r"\.0$", "", regex=True)


def _client_accounts(data_dir: str, manifest: dict, used: list) -> pd.DataFrame:
    ids: set = set()
    for e in store.of_kind(manifest, "clients"):
        ids |= set(_ids(pd.read_csv(store.path_of(data_dir, e), usecols=["CONT_ID"], dtype=str)["CONT_ID"]))
        used.append(e["file"])
    frames = []
    for e in store.of_kind(manifest, "client_transactions"):
        frames.append(pd.read_csv(store.path_of(data_dir, e), usecols=["CONT_ID", "TX_TOTAL_AMT", "DATE_CREDIT"],
                                  dtype=str))
        used.append(e["file"])
    tx = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["CONT_ID", "TX_TOTAL_AMT",
                                                                                     "DATE_CREDIT"])
    tx = tx.assign(cid=_ids(tx["CONT_ID"].astype(str)),
                   amt=pd.to_numeric(tx["TX_TOTAL_AMT"], errors="coerce").clip(lower=0).fillna(0.0),
                   when=pd.to_datetime(tx["DATE_CREDIT"], utc=True, errors="coerce", format="mixed"))
    tx = tx[tx["when"].notna()]
    ids |= set(tx["cid"])
    if not ids:
        return pd.DataFrame(columns=["acct", "bank_id", "segment", "peak", "first", "tier"])
    tx = tx.assign(month=tx["when"].dt.year * 12 + tx["when"].dt.month, day=tx["when"].dt.floor("D"))
    order = sorted(ids)
    df = pd.DataFrame({
        "acct": ["client-" + i for i in order],
        "monthly": tx.groupby(["cid", "month"])["amt"].sum().groupby(level="cid").mean().reindex(order).to_numpy(),
        "peak": tx.groupby(["cid", "day"])["amt"].sum().groupby(level="cid").max().reindex(order).fillna(0.0)
        .to_numpy(),
        "first": (tx.groupby("cid")["when"].min().reindex(order) - EPOCH).dt.total_seconds().to_numpy(),
    })
    df["segment"] = _segments(df["monthly"])
    df["bank_id"] = "client"
    df["tier"] = "low"
    return df


def _replay(path: str, start_ts: int, limit: int) -> pd.DataFrame:
    """The earliest `limit` payments at or after `start_ts`, kept while streaming so memory stays bounded."""
    kept, offset = None, 0
    for chunk in _card_chunks(path):
        f = card_frame(chunk)
        f["row"] = np.arange(offset, offset + len(f))
        offset += len(f)
        t = f[(f["ts"] >= start_ts) & (f["amount"] > 0)]
        if len(t):
            kept = t if kept is None else pd.concat([kept, t], ignore_index=True)
            if len(kept) > limit:
                kept = kept.nsmallest(limit, ["ts", "row"])
    if kept is None:
        return pd.DataFrame(columns=["txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format", "label"])
    kept = kept.sort_values(["ts", "row"]).reset_index(drop=True)
    return pd.DataFrame({"txn_id": [f"t-{i + 1:09d}" for i in range(len(kept))], "ts": kept["ts"],
                         "from_acct": kept["acct"], "to_acct": kept["payee"], "amount": kept["amount"].round(2),
                         "currency": "USD", "format": kept["fmt"], "label": kept["label"]})


def _iso(ts) -> str:
    return pd.Timestamp(int(ts), unit="s", tz="UTC").strftime("%Y-%m-%d")


def build(data_dir: str, replay_rows: int = REPLAY_ROWS) -> dict:
    if not 0 < replay_rows < 900_000_000:
        raise VaultError("--replay-rows must be between 1 and 899,999,999 (replay ids stay below t-900000000)")
    check = store.verify(data_dir)
    if not check["ok"]:
        raise VaultError(f"the vault changed since it was sealed (changed {check['changed']}, missing "
                         f"{check['missing']}, added {check['added']}); reseal deliberately or restore the files")
    manifest = store.load_manifest(data_dir)
    report: dict = {"built_ts": time.time(), "manifest_digest": manifest["digest"], "command": "python -m vault build",
                    "used": [], "ignored": [], "replay": None}
    parts = []
    cards = sorted(store.of_kind(manifest, "card_transactions"), key=lambda e: -e.get("rows", 0))
    if cards:
        card, report["ignored"] = cards[0], [e["file"] for e in cards[1:]]
        path = store.path_of(data_dir, card)
        codes = _Codes()
        daily = _card_daily(path, codes)
        last_train, last_val = _split_days(daily)
        parts.append(_card_accounts(daily, codes, last_train, last_val))
        report["used"].append(card["file"])
        report["split"] = {"train_to": _iso(last_train * DAY_S), "validation_to": _iso(last_val * DAY_S),
                           "test_from": _iso((last_val + 1) * DAY_S)}
        replay = _replay(path, (last_val + 1) * DAY_S, replay_rows)
        out = os.path.join(data_dir, "replay", REPLAY_NAME)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        replay.to_csv(out, index=False)
        report["replay"] = {"file": f"replay/{REPLAY_NAME}", "rows": len(replay), "fraud": int(replay["label"].sum()),
                            "from": _iso(replay["ts"].min()) if len(replay) else None,
                            "to": _iso(replay["ts"].max()) if len(replay) else None}
    clients = _client_accounts(data_dir, manifest, report["used"])
    if len(clients):
        parts.append(clients)
    if not parts:
        raise VaultError("the vault holds no card or client files, so there are no accounts to build")
    acc = pd.concat(parts, ignore_index=True).sort_values("acct").reset_index(drop=True)
    acc["opening_balance"] = np.maximum(MIN_BALANCE, BALANCE_DAYS * acc["peak"].astype(float)).round(2)
    acc["first_seen_ts"] = [str(int(v)) if pd.notna(v) else "" for v in acc["first"]]
    bank_dir = os.path.join(data_dir, "bank")
    os.makedirs(bank_dir, exist_ok=True)
    acc[["acct", "bank_id", "segment", "opening_balance", "first_seen_ts"]].to_csv(
        os.path.join(bank_dir, "accounts.csv"), index=False)
    acc[["acct", "tier"]].to_csv(os.path.join(bank_dir, "tiers.csv"), index=False)
    report["accounts"] = {"total": len(acc), "by_source": acc["bank_id"].value_counts().to_dict(),
                          "by_segment": acc["segment"].value_counts().to_dict()}
    report["tiers"] = acc["tier"].value_counts().to_dict()
    report["files"] = {"accounts": "bank/accounts.csv", "tiers": "bank/tiers.csv"}
    store.write_build(data_dir, report)
    return report
