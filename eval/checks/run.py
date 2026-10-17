"""python -m eval.checks.run <C-xx|all>   (team/CHECKS.md)

Each check prints one line: PASS C-xx <name>, FAIL C-xx <name>: <reason>, or SKIP C-xx <name>: <why> (a skip is never
a pass). Exit code: 0 if nothing failed. `all` runs every check in order and stops nothing early.
Checks talk only to 127.0.0.1 (the edge, the bank, Valkey) and read local files.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OFFSET = int(os.environ.get("RF_PORT_OFFSET", "0"))
DATA = os.environ.get("RF_DATA", "/srv/riskfabric/data")
EDGE = f"http://127.0.0.1:{8080 + OFFSET}"
BANK = os.environ.get("RF_ENGINE_URL", f"http://127.0.0.1:{9000 + OFFSET}")
SAMPLE = {"txn_id": "t-000123456", "ts": 1792310400.123, "from_acct": "8000EBD30", "to_acct": "8000F5340",
          "amount": 1250.0, "currency": "USD", "format": "ACH"}
RESPONSE_TYPES = {"contract": int, "txn_id": str, "action": str, "path": str, "stand_in": bool, "reasons": list,
                  "latency_ms": (int, float), "deadline_ms": int, "deadline_met": bool, "mode": str}
NULLABLE = {"score": (int, float), "model": str, "tier": str, "cluster": str, "load": str, "engine": dict}
STREAM_FIELDS = {
    "edge.log": "ts req_id ip fp asn method path status ms backend cluster edge_action",
    "decisions": "ts txn_id req_id action path stand_in score model tier cluster load latency_ms deadline_met mode replica",
    "health": "ts psi_cpu psi_mem p99_ms retrans_s health_score",
    "threat": "ts seq level clusters",
    "bank.log": "ts txn_id req_id entry action latency_ms queue_ms timeout rules posted",
    "payments": "ts txn_id from_acct to_acct amount currency format",
}


class Fail(Exception):
    pass


class Skip(Exception):
    pass


def _http():
    import httpx

    return httpx.Client(timeout=3.0)


def _redis():
    import redis

    return redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)


def _txn() -> str:
    return f"t-{900000000 + (uuid.uuid4().int % 99999999):09d}"


def _pay(client, body=None, headers=None):
    """One payment as a well-behaved known customer. Each call uses a different client IP, so the checks' bursts
    never trip the edge's per-IP limit, and `X-Client-Known: 1` keeps Sentinel from treating the checks as a cluster."""
    n = uuid.uuid4().int
    h = {"X-Request-Id": str(uuid.uuid4()), "X-Forwarded-For": f"198.18.{n % 250}.{(n >> 8) % 250 + 1}",
         "X-Client-FP": "fp-check0001", "X-Client-ASN": "64512", "X-Client-Known": "1"}
    h.update(headers or {})
    return client.post(f"{EDGE}/pay", json=body or dict(SAMPLE, txn_id=_txn()), headers=h)


# ---------------- the checks ----------------
def c01():
    """Ports"""
    ports = {"edge": 8080, "valkey": 6379, "bank": 9000, "console": 8500}
    if os.environ.get("RF_CHECK_SYSTEM", "shield") not in ("bank_only", "bank_realistic"):
        ports["decision primary"] = 8001
    down = []
    for name, port in ports.items():
        p = port if name == "valkey" else port + OFFSET
        try:
            socket.create_connection(("127.0.0.1", p), timeout=1).close()
        except OSError:
            down.append(f"{name}:{p}")
    if down:
        raise Fail("not answering: " + ", ".join(down))


def c02():
    """Pay contract"""
    with _http() as c:
        r = _pay(c)
    if r.status_code != 200:
        raise Fail(f"status {r.status_code}")
    body = r.json()
    for k, t in RESPONSE_TYPES.items():
        if k not in body or not isinstance(body[k], t) or (t is int and isinstance(body[k], bool)):
            raise Fail(f"field {k!r} missing or wrong type: {body.get(k)!r}")
    for k, t in NULLABLE.items():
        if k not in body or (body[k] is not None and not isinstance(body[k], t)):
            raise Fail(f"field {k!r} missing or wrong type: {body.get(k)!r}")
    checks = [(body["contract"] == 2, "contract is not 2"), (body["action"] in ("ALLOW", "VERIFY", "HOLD"), "bad action"),
              (body["path"] in ("full", "stand_in", "engine", "bank"), "bad path"),
              (body["mode"] in ("standalone", "shield", "bank_only"), "bad mode"), (len(body["reasons"]) <= 5, "too many reasons")]
    for ok, why in checks:
        if not ok:
            raise Fail(why)


def c03():
    """Latency"""
    limit = float(os.environ.get("RF_CHECK_P99_MS", "50"))
    lat = []
    with _http() as c:
        for _ in range(200):
            t0 = time.perf_counter()
            r = _pay(c)
            lat.append(1000 * (time.perf_counter() - t0))
            if r.status_code != 200:
                raise Fail(f"status {r.status_code}")
    p99 = sorted(lat)[int(0.99 * (len(lat) - 1))]
    if p99 > limit:
        raise Fail(f"p99 {p99:.1f} ms > {limit:g} ms")
    print(f"     p99 {p99:.1f} ms over 200 sequential requests")


def c04():
    """Streams"""
    r = _redis()
    with _http() as c:
        for _ in range(20):
            _pay(c)
    time.sleep(2)
    want = os.environ.get("RF_CHECK_STREAMS", "edge.log,decisions,health,threat,bank.log,payments").split(",")
    for s in want:
        entries = r.xrevrange(s, count=20)
        need = 20 if s in ("edge.log", "decisions") else 1
        if len(entries) < need:
            raise Fail(f"{s}: {len(entries)} entries, need {need}")
        missing = set(STREAM_FIELDS[s].split()) - set(entries[0][1])
        if missing:
            raise Fail(f"{s}: fields missing {sorted(missing)}")
        if s in ("health", "threat"):
            age = time.time() - float(entries[0][1]["ts"])
            if age > (5 if s == "health" else 2.5):
                raise Fail(f"{s}: newest entry is {age:.1f} s old")


def c05():
    """Ledger"""
    from ledger.verify import verify_file

    path = os.path.join(DATA, "ledger", "ledger.jsonl")
    res = verify_file(path)
    if not res["ok"]:
        raise Fail(f"verify failed: seq {res['bad_seq']}: {res['reason']}")
    if res["records"] < 100:
        raise Fail(f"only {res['records']} records; run 100 decisions first")
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "ledger.jsonl")
        shutil.copy2(path, copy)
        data = bytearray(open(copy, "rb").read())
        lines = data.split(b"\n")
        target = 49  # seq 50
        idx = lines[target].find(b'"action":"') + 10
        if idx < 10:
            idx = len(lines[target]) // 2
        lines[target][idx:idx + 1] = b"X" if lines[target][idx:idx + 1] != b"X" else b"Y"
        open(copy, "wb").write(b"\n".join(lines))
        bad = verify_file(copy)
        if bad["ok"] or bad["bad_seq"] != 50:
            raise Fail(f"tamper test: expected seq 50 to fail, got {bad}")


def c06():
    """Edge limits"""
    import httpx

    r = _redis()
    with _http() as c:
        got429, end = False, time.time() + 2.5
        while time.time() < end and not got429:
            if _pay(c, headers={"X-Forwarded-For": "198.19.250.250"}).status_code == 429:
                got429 = True
        if not got429:
            raise Fail("no 429 from one IP within 2 s")
        newest = r.xrevrange("threat", count=1)
        if newest and time.time() - float(newest[0][1].get("ts", 0)) < 3:
            raise Skip("per-IP limit OK; Sentinel is writing threat entries, which replace the test block. "
                       "Stop Sentinel (or ask D) and re-run C-06 for the block-map part")
        fp = f"fp-chk{uuid.uuid4().hex[:6]}"
        now = time.time()
        r.xadd("threat", {"ts": str(now), "seq": "999999", "level": "COORDINATED", "clusters": json.dumps(
            [{"id": "c-check", "key_type": "fp", "key": fp, "rps": 1, "share": 0.5, "decline_rate": 0, "risk": 1.0,
              "action": "block", "until": now + 30}])})
        blocked, end = False, time.time() + 2.5
        while time.time() < end and not blocked:
            blocked = _pay(c, headers={"X-Client-FP": fp, "X-Forwarded-For": "198.19.1.1"}).status_code == 403
            time.sleep(0.1)
        if not blocked:
            raise Fail("fingerprint in the block map did not give 403 within 2 s")
        r.xadd("threat", {"ts": str(time.time()), "seq": "1000000", "level": "NORMAL", "clusters": "[]"})
        released, end = False, time.time() + 3
        while time.time() < end and not released:
            released = _pay(c, headers={"X-Client-FP": fp, "X-Forwarded-For": "198.19.1.2"}).status_code == 200
            time.sleep(0.1)
        if not released:
            raise Fail("removing the fingerprint did not give 200 again")


def c07():
    """Stand-in drill"""
    r = _redis()
    accounts = {"low": "chk-low", "medium": "chk-medium", "high": "chk-high"}
    for tier, acct in accounts.items():
        r.set(f"tier:{acct}", tier)
    r.set("cfg:force_timeout", "1")
    try:
        cases = [("chk-low", 50.0, "ALLOW", "low"), ("chk-low", 10 ** 7, "VERIFY", "low"), ("chk-medium", 50.0, "VERIFY", "medium"),
                 ("chk-high", 50.0, "HOLD", "high"), ("chk-unknown-" + uuid.uuid4().hex[:6], 50.0, "HOLD", "unknown")]
        with _http() as c:
            for acct, amount, want, tier in cases:
                b = _pay(c, dict(SAMPLE, txn_id=_txn(), from_acct=acct, amount=amount)).json()
                if b.get("action") != want or b.get("stand_in") is not True or b.get("score") is not None or b.get("tier") != tier:
                    raise Fail(f"{tier} {amount}: got {b.get('action')}, stand_in {b.get('stand_in')}, tier {b.get('tier')}")
    finally:
        r.set("cfg:force_timeout", "0")


def c08():
    """Replay file"""
    import csv

    path = os.path.join(DATA, "replay", "hi_small_test.csv")
    if not os.path.exists(path):
        raise Fail(f"{path} not found")
    header = "txn_id,ts,from_acct,to_acct,amount,currency,format,label".split(",")
    seen, last_ts, n = set(), float("-inf"), 0
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != header:
            raise Fail(f"header {reader.fieldnames}")
        for row in reader:
            n += 1
            ts = float(row["ts"])
            if ts < last_ts:
                raise Fail(f"row {n} not sorted by ts")
            last_ts = ts
            if row["label"] not in ("0", "1"):
                raise Fail(f"row {n} label {row['label']!r}")
            tid = row["txn_id"]
            if not re.fullmatch(r"t-\d{9}", tid) or int(tid[2:]) >= 900000000:
                raise Fail(f"row {n} txn_id {tid!r}")
            if tid in seen:
                raise Fail(f"duplicate txn_id {tid}")
            seen.add(tid)
    if n < 10000:
        raise Fail(f"only {n} rows")


def c09():
    """Threat"""
    r = _redis()
    entries = r.xrevrange("threat", count=10)
    if len(entries) < 10:
        raise Fail("fewer than 10 threat entries")
    ts = sorted(float(e[1]["ts"]) for e in entries)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    if any(g > 2.5 for g in gaps):
        raise Fail(f"gap of {max(gaps):.1f} s between threat entries")
    for _, f in entries:
        for c in json.loads(f.get("clusters") or "[]"):
            if not {"key_type", "risk", "until"} <= set(c):
                raise Fail(f"cluster {c.get('id')} lacks key_type, risk or until")
    print("     scenario parts (S3 cluster within 10 s, S2 never COORDINATED) are checked from the result files: M-17, M-18")


def c10():
    """Shield"""
    import httpx

    with _http() as c:
        body = dict(SAMPLE, txn_id=_txn())
        eng = c.post(f"{BANK}/engine/pay", json=body).json()
        b = _pay(c, body=dict(SAMPLE, txn_id=_txn()))
        b = b.json()
        if b.get("mode") != "shield":
            raise Skip("stack is not in Shield mode")
        if b.get("path") != "engine" or not b.get("engine") or b["engine"].get("decision") != b.get("action"):
            raise Fail(f"engine decision not returned unchanged: {b}")
    print(f"     engine said {eng.get('decision')}; delay and failure switches need the router to pass X-Mock-* (run by hand)")


def c11():
    """End-to-end smoke"""
    r = _redis()
    marker = time.time()
    ids = []
    with _http() as c:
        for _ in range(100):
            rid = str(uuid.uuid4())
            resp = _pay(c, headers={"X-Request-Id": rid})
            if resp.status_code != 200:
                raise Fail(f"status {resp.status_code}")
            ids.append(rid)
    time.sleep(3)
    want = set(ids)
    edge = {f.get("req_id") for _, f in r.xrevrange("edge.log", count=5000) if float(f.get("ts", 0)) >= marker - 1}
    dec = {f.get("req_id") for _, f in r.xrevrange("decisions", count=5000) if float(f.get("ts", 0)) >= marker - 1}
    if not want <= edge:
        raise Fail(f"{len(want - edge)} requests missing from edge.log")
    if not want <= dec:
        raise Fail(f"{len(want - dec)} requests missing from decisions")
    path = os.path.join(DATA, "ledger", "ledger.jsonl")
    recs = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"kind":"decision"' in line:
                    recs.add(json.loads(line).get("req_id"))
    if not want <= recs:
        raise Fail(f"{len(want - recs)} requests missing from the ledger")


SECRET_PATTERNS = [r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"IBM_QUANTUM_TOKEN\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}",
                   r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{16,}['\"]", r"AKIA[0-9A-Z]{16}"]


def c12():
    """Secrets"""
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    if not tracked:
        raise Skip("not a git repository yet")
    bad = [f for f in tracked if os.path.basename(f).startswith(".env") and not f.endswith(".example")]
    if bad:
        raise Fail(f".env files tracked: {bad}")
    history = subprocess.run(["git", "log", "-p", "--all", "--no-color"], cwd=REPO, capture_output=True, text=True,
                             errors="replace").stdout
    for pat in SECRET_PATTERNS:
        m = re.search(pat, history)
        if m:
            raise Fail(f"possible secret in git history matching {pat!r}: {m.group(0)[:40]}...")


def c13():
    """Scoring"""
    import csv
    import importlib

    try:
        scoring = importlib.import_module("scoring")
    except ImportError as exc:
        raise Fail(f"cannot import scoring: {exc}")
    scoring.load(os.path.join(DATA, "models"))
    path = os.path.join(DATA, "replay", "hi_small_test.csv")
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if i >= 1000:
                break
            rows.append({"txn_id": row["txn_id"], "ts": float(row["ts"]), "from_acct": row["from_acct"], "to_acct": row["to_acct"],
                         "amount": float(row["amount"]), "currency": row["currency"], "format": row["format"]})
    signals = {"cluster": None, "client_rate": None, "edge_action": None}
    lat, first = [], []
    for p in rows:
        t0 = time.perf_counter()
        out = scoring.score(p, signals, 40)
        lat.append(1000 * (time.perf_counter() - t0))
        if not (0 <= out["score"] <= 1 and isinstance(out["reasons"], list) and len(out["reasons"]) <= 5 and out.get("model")):
            raise Fail(f"bad output for {p['txn_id']}: {out}")
        first.append(out["score"])
    warm = sorted(lat[100:])
    p99 = warm[int(0.99 * (len(warm) - 1))]
    if p99 > 20:
        raise Fail(f"warm p99 {p99:.2f} ms > 20 ms")
    print(f"     warm p99 {p99:.2f} ms; the idle-versus-stress comparison is run by hand with stress-ng (fresh load each time)")


def c14():
    """Books reconcile"""
    import httpx

    with _http() as c:
        p = dict(SAMPLE, txn_id=_txn(), amount=1.0, from_acct="chk-recon", to_acct="chk-recon-2")
        body = {"payment": p, "action": "ALLOW", "path": "engine", "via": "shield", "req_id": str(uuid.uuid4())}
        a = c.post(f"{BANK}/bank/settle", json=body).json()
        b = c.post(f"{BANK}/bank/settle", json=body).json()
    if not a.get("posted") or a.get("duplicate") or not b.get("duplicate"):
        raise Fail(f"settle not idempotent: first {a}, second {b}")
    path = os.path.join(DATA, "bank", "postings.jsonl")
    n = sum(1 for line in open(path, encoding="utf-8") if p["txn_id"] in line) if os.path.exists(path) else 0
    if n != 1:
        raise Fail(f"{n} postings for one settled txn")
    print("     the per-run reconcile (M-20) is in every result file")


def c15():
    """Quantum isolation"""
    for folder in ("decision", "scoring", "bank"):
        for root, _, files in os.walk(os.path.join(REPO, folder)):
            for name in files:
                if name.endswith(".py"):
                    text = open(os.path.join(root, name), encoding="utf-8", errors="replace").read()
                    if re.search(r"import\s+qiskit|from\s+qiskit|from\s+quantum|import\s+quantum|openai|anthropic", text):
                        raise Fail(f"quantum or LLM import in {folder}/{name}")
    from quantum.worker.worker import gate_allows_rerank

    gate = os.path.join(REPO, "quantum", "GATE.md")
    if os.environ.get("RF_QML_MODE") == "rerank" and not gate_allows_rerank(gate):
        raise Fail("RF_QML_MODE=rerank but GATE.md does not allow it")
    r = _redis()
    holds = r.zrevrange("hold:queue", 0, 19)
    missing = [t for t in holds if not r.exists(f"qml:{t}")]
    if holds and missing:
        raise Fail(f"{len(missing)} of the last {len(holds)} HOLDs have no qml:{{txn_id}}")
    print("     part 2 (identical actions with the worker on and off) and part 5 (numpy vs Qiskit) are run by hand: "
          "`python -m quantum.research.evaluate checks`")


def c16():
    """Results provenance"""
    import glob

    files = glob.glob(os.path.join(REPO, "eval", "results", "*__*__*.json")) + glob.glob(
        os.path.join(REPO, "quantum", "results", "*__seed*__*.json"))
    if not files:
        raise Skip("no result files yet")
    run_ids = set()
    for path in files:
        d = json.load(open(path, encoding="utf-8"))
        for key in ("command", "commit"):
            if not d.get(key):
                raise Fail(f"{os.path.basename(path)} lacks {key}")
        if "quantum" in path and not d.get("protocol_commit"):
            raise Fail(f"{os.path.basename(path)} lacks protocol_commit")
        if d.get("run_id"):
            run_ids.add(d["run_id"])
    summary = os.path.join(REPO, "eval", "results", "summary.json")
    if os.path.exists(summary):
        s = json.load(open(summary, encoding="utf-8"))
        for scen, cells in s.get("matrix", {}).items():
            for system, cell in cells.items():
                unknown = set(cell.get("runs", [])) - run_ids
                if unknown:
                    raise Fail(f"summary {scen}/{system} lists missing runs {sorted(unknown)}")


CHECKS = [("C-01", c01), ("C-02", c02), ("C-03", c03), ("C-04", c04), ("C-05", c05), ("C-06", c06), ("C-07", c07),
          ("C-08", c08), ("C-09", c09), ("C-10", c10), ("C-11", c11), ("C-12", c12), ("C-13", c13), ("C-14", c14),
          ("C-15", c15), ("C-16", c16)]


def run(ids: list) -> int:
    failed = 0
    for cid, fn in CHECKS:
        if ids != ["all"] and cid not in ids:
            continue
        name = fn.__doc__
        try:
            fn()
            print(f"PASS {cid} {name}")
        except Skip as exc:
            print(f"SKIP {cid} {name}: {exc}")
        except Fail as exc:
            failed += 1
            print(f"FAIL {cid} {name}: {exc}")
        except Exception as exc:  # a service that is down is a failure, with the reason
            failed += 1
            print(f"FAIL {cid} {name}: {exc.__class__.__name__}: {str(exc)[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:] or ["all"]))
