"""Vault tests on SYNTHETIC look-alike files: sealing, tamper detection, recognition by header, and the bank build."""
import csv
import json
import os

import pytest

from vault import __main__ as cli
from vault import bankdata, store
from vault.store import VaultError
from vault.tests import lookalike


@pytest.fixture
def data(tmp_path):
    info = lookalike.write(str(tmp_path / "vault"))
    return str(tmp_path), info


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_seal_records_every_file_by_kind(data):
    d, info = data
    m = store.seal(d)
    kinds = {e["file"]: e["kind"] for e in m["files"]}
    assert kinds == {"transactions_history.csv": "card_transactions", "clients.csv": "clients",
                     "transactions.csv": "client_transactions", "sample_data/sample_loan.csv": "loans",
                     "sample_data/w2/1.pdf": "document"}
    card = next(e for e in m["files"] if e["kind"] == "card_transactions")
    assert card["rows"] == info["card_rows"] and len(card["sha256"]) == 64
    with pytest.raises(VaultError, match="already sealed"):
        store.seal(d)
    assert store.seal(d, reseal=True)["replaces"] == m["digest"]


def test_verify_names_changed_missing_and_added_files(data):
    d, _ = data
    store.seal(d)
    assert store.verify(d)["ok"] is True
    vroot = store.root(d)
    with open(os.path.join(vroot, "clients.csv"), "a", encoding="utf-8") as fh:
        fh.write("9999,1,30,1,1,0\n")
    os.remove(os.path.join(vroot, "sample_data", "w2", "1.pdf"))
    with open(os.path.join(vroot, "extra.csv"), "w", encoding="utf-8") as fh:
        fh.write("a,b\n1,2\n")
    v = store.verify(d)
    assert (v["ok"], v["changed"], v["missing"], v["added"]) == (False, ["clients.csv"], ["sample_data/w2/1.pdf"],
                                                                  ["extra.csv"])
    with pytest.raises(VaultError, match="changed since it was sealed"):
        bankdata.build(d)


def test_files_are_recognised_by_header_not_name(tmp_path):
    vroot = tmp_path / "vault"
    lookalike.write(str(vroot))
    os.rename(vroot / "transactions_history.csv", vroot / "cards_renamed.csv")
    with open(vroot / "clients.csv", "w", encoding="utf-8") as fh:
        fh.write("CONT_ID,AGE_YEARS\n1,40\n")
    m = store.seal(str(tmp_path))
    by = {e["file"]: e for e in m["files"]}
    assert by["cards_renamed.csv"]["kind"] == "card_transactions"
    assert by["clients.csv"]["kind"] == "other" and by["clients.csv"]["expected_kind"] == "clients"
    assert "ANNUAL_INCOME" in by["clients.csv"]["missing_columns"]


def test_build_writes_registry_tiers_and_a_time_ordered_replay(data):
    d, info = data
    store.seal(d)
    report = bankdata.build(d, replay_rows=10_000)
    accounts = _read(os.path.join(d, "bank", "accounts.csv"))
    tiers = {r["acct"]: r["tier"] for r in _read(os.path.join(d, "bank", "tiers.csv"))}
    assert list(accounts[0]) == ["acct", "bank_id", "segment", "opening_balance", "first_seen_ts"]
    cards = [a for a in accounts if a["bank_id"] == "card"]
    clients = [a for a in accounts if a["bank_id"] == "client"]
    assert len(cards) == info["card_accounts"] and len(clients) == info["clients"]
    assert all(float(a["opening_balance"]) >= 1000 for a in accounts)
    assert {a["segment"] for a in accounts} <= {"retail", "business"}
    assert 0 < sum(a["segment"] == "business" for a in cards) <= len(cards) * 0.15
    risky = {acct for acct, t in tiers.items() if t in ("high", "medium")}
    assert risky and all(acct.split("-")[1] in ("3", "7") for acct in risky)   # only the fraud users' cards
    assert all(tiers[a["acct"]] == "low" for a in clients)
    replay = _read(os.path.join(d, "replay", bankdata.REPLAY_NAME))
    assert list(replay[0]) == ["txn_id", "ts", "from_acct", "to_acct", "amount", "currency", "format", "label"]
    ts = [int(r["ts"]) for r in replay]
    assert ts == sorted(ts) and all(float(r["amount"]) > 0 for r in replay)
    assert [r["txn_id"] for r in replay[:3]] == ["t-000000001", "t-000000002", "t-000000003"]
    assert {r["format"] for r in replay} <= {"swipe", "chip", "online"} and replay[0]["to_acct"].startswith("merchant-")
    assert report["replay"]["rows"] == len(replay) and report["replay"]["from"] >= report["split"]["test_from"]
    assert report["manifest_digest"] == store.load_manifest(d)["digest"]
    assert json.load(open(os.path.join(d, "vault", "BUILD.json")))["accounts"]["total"] == len(accounts)


def test_only_the_larger_card_file_is_used(data):
    d, _ = data
    vroot = store.root(d)
    with open(os.path.join(vroot, "transactions_history.csv"), encoding="utf-8") as src, \
            open(os.path.join(vroot, "test_220_40k.csv"), "w", encoding="utf-8") as dst:
        lines = src.readlines()
        dst.write("Index," + lines[0])
        for i, line in enumerate(lines[1:200]):
            dst.write(f"{i},{line}")
    store.seal(d)
    report = bankdata.build(d, replay_rows=100)
    assert "transactions_history.csv" in report["used"] and report["ignored"] == ["test_220_40k.csv"]
    assert report["replay"]["rows"] == 100


def test_cli_exit_codes(data, capsys):
    d, _ = data
    assert cli.main(["verify", "--data", d]) == 1       # not sealed yet
    assert cli.main(["seal", "--data", d]) == 0
    assert cli.main(["verify", "--data", d]) == 0
    assert cli.main(["build", "--data", d, "--replay-rows", "50"]) == 0
    assert '"rows": 50' in capsys.readouterr().out
