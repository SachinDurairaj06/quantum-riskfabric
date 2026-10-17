"""Ledger tests: chain, tamper detection (C-05), checkpoints, restart recovery."""
import json

import fakeredis

from ledger.chain import CHECKPOINT_EVERY
from ledger.verify import verify_file
from ledger.writer import CURSOR_KEY, HEAD_KEY, LedgerWriter


def feed(r, n, start=0):
    for i in range(start, start + n):
        rec = {"ts": 1.0 + i, "kind": "decision", "txn_id": f"t-{i:09d}", "action": "ALLOW", "score": 0.1}
        r.xadd("ledger.in", {"rec": json.dumps(rec)})


def drain(writer, last="0"):
    while True:
        new = writer.step(last, block_ms=1)
        if new == last:
            return last
        last = new


def test_writer_chain_verifies_and_sets_head(tmp_path):
    r = fakeredis.FakeRedis(decode_responses=True)
    path = str(tmp_path / "ledger.jsonl")
    feed(r, 100)
    w = LedgerWriter(r, path)
    drain(w)
    result = verify_file(path)
    assert result["ok"] and result["records"] == 100
    assert r.get(HEAD_KEY) == result["head"] and r.get(CURSOR_KEY)
    first = json.loads(open(path, encoding="utf-8").readline())
    assert first["seq"] == 1 and first["prev_hash"] == "0" * 64


def test_flipped_byte_names_the_bad_seq(tmp_path):
    r = fakeredis.FakeRedis(decode_responses=True)
    path = tmp_path / "ledger.jsonl"
    feed(r, 50)
    drain(LedgerWriter(r, str(path)))
    lines = path.read_bytes().splitlines(keepends=True)
    target = lines[29]  # seq 30
    idx = target.index(b'"ALLOW"') + 1
    lines[29] = target[:idx] + b"B" + target[idx + 1:]
    tampered = tmp_path / "copy.jsonl"
    tampered.write_bytes(b"".join(lines))
    result = verify_file(str(tampered))
    assert not result["ok"] and result["bad_seq"] == 30


def test_checkpoints_are_written_and_verified(tmp_path):
    r = fakeredis.FakeRedis(decode_responses=True)
    path = tmp_path / "ledger.jsonl"
    feed(r, CHECKPOINT_EVERY + 5)
    drain(LedgerWriter(r, str(path)))
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    cps = [x for x in recs if x["kind"] == "checkpoint"]
    assert len(cps) == 1 and cps[0]["seq_range"] == [1, CHECKPOINT_EVERY] and cps[0]["sig"] is None
    assert verify_file(str(path))["ok"]


def test_restart_continues_the_chain_without_duplicates(tmp_path):
    r = fakeredis.FakeRedis(decode_responses=True)
    path = str(tmp_path / "ledger.jsonl")
    feed(r, 10)
    w = LedgerWriter(r, path)
    drain(w, w.start_id())
    feed(r, 5, start=10)
    w2 = LedgerWriter(r, path)
    drain(w2, w2.start_id())
    result = verify_file(path)
    assert result["ok"] and result["records"] == 15


def test_nan_becomes_null(tmp_path):
    r = fakeredis.FakeRedis(decode_responses=True)
    path = tmp_path / "ledger.jsonl"
    w = LedgerWriter(r, str(path))
    w.append([{"ts": 1.0, "kind": "decision", "score": float("nan")}])
    assert json.loads(path.read_text(encoding="utf-8"))["score"] is None
    assert verify_file(str(path))["ok"]
