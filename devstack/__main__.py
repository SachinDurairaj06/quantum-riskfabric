"""python -m devstack <up|demo|edge>: the whole bank chain on one machine, for laptops without HAProxy or Valkey.

  up     start every process for --system and keep them running until Ctrl+C
  demo   start them, send a short, capped scenario through the perimeter, print the numbers, stop everything
  edge   run only the edge stand-in (up and demo start it for you)

The chain: lab -> perimeter :8000 -> edge :8080 -> decision :8001 (shield only) -> bank :9000, with Sentinel and the
ledger writer alongside for shield, and the console on :8500. The edge is devstack/edge.py, a stand-in for D's HAProxy. Valkey is the one at
RF_VALKEY_URL if it answers; otherwise an in-memory stand-in (fakeredis, for development only, lost on stop). Ports
add RF_PORT_OFFSET, everything listens on 127.0.0.1, and the data folder (RF_DATA or --data) must hold a built vault.
Standalone and the downgrade baseline need A's scoring package, so they aren't offered yet.

A laptop run is a working check, not a measurement: numbers for the README come from the LinuxONE VM.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEMS = {"bank_only": "statusquo", "bank_realistic": "realistic", "shield": "riskfabric"}
KEY_PREFIXES = ("tier:", "hold:", "qml:", "bank:", "ledger:", "cfg:")
KEEP = ("cfg:perimeter_ip_rps", "cfg:perimeter_limit_rps")       # perimeter policy survives a reset
STREAMS = ("edge.log", "decisions", "payments", "health", "threat", "bank.log", "ledger.in")
CFG_DEFAULTS = {"cfg:deadline_ms": "100", "cfg:hold_cap_pct": "2", "cfg:standin_low_limit": "10000"}
SERVICE_MS_TARGET = 10.0


def slots_for(base_rps: float) -> int:
    """Slots so the 4R capacity rule gives a service time near 10 ms, as with 8 slots at R = 200 on the VM.
    At a laptop's low R, 8 slots would mean a service time near the whole 100 ms deadline."""
    return max(1, round(4 * base_rps * SERVICE_MS_TARGET / 1000.0))


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


class Stack:
    def __init__(self, system: str, data: str, offset: int, url: str):
        self.system, self.data, self.offset, self.url = system, os.path.abspath(data), offset, url
        self.run_dir = os.path.abspath(os.environ.get("RF_RUN") or os.path.join(self.data, "run"))
        self.logs = os.path.join(self.run_dir, "logs")
        os.makedirs(self.logs, exist_ok=True)
        self.procs: list = []
        self.fake_valkey = None

    def env(self, **extra) -> dict:
        path = os.pathsep.join(p for p in (REPO, os.environ.get("PYTHONPATH", "")) if p)
        return dict(os.environ, RF_VALKEY_URL=self.url, RF_DATA=self.data, RF_PORT_OFFSET=str(self.offset),
                    RF_RUN=self.run_dir, PYTHONPATH=path, PYTHONUNBUFFERED="1", **extra)

    def port(self, base: int) -> int:
        return base + self.offset

    def redis(self):
        import redis

        return redis.Redis.from_url(self.url, decode_responses=True)

    def ensure_valkey(self, allow_reset: bool):
        r = self.redis()
        try:
            real = bool(r.ping())
        except Exception:
            real = False
        if real:
            if not allow_reset:
                raise SystemExit(f"devstack: a real Valkey answers at {self.url}. A run clears RiskFabric's keys in "
                                 "that database; pass --allow-reset if that is fine (never on the shared stack).")
            return r
        parsed = urlparse(self.url)
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit(f"devstack: no Valkey at {self.url}, and the stand-in only runs on 127.0.0.1")
        log = open(os.path.join(self.logs, "valkey-standin.log"), "w")
        self.fake_valkey = subprocess.Popen([sys.executable, "-c", (
            "import fakeredis,sys; fakeredis.TcpFakeServer(('127.0.0.1', int(sys.argv[1])), "
            "server_type='redis').serve_forever()"), str(parsed.port or 6379)], stdout=log, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                r.ping()
                print(f"devstack: in-memory Valkey stand-in on {self.url} (development only)")
                return r
            except Exception:
                time.sleep(0.1)
        raise SystemExit("devstack: the Valkey stand-in did not start; see " + log.name)

    def reset(self, r, base_rps: float) -> dict:
        """What D's reset.sh does, for this machine: clear RiskFabric's keys and streams, then set the run's config."""
        from bank.load import load
        from eval.scenarios.runner import bank_service_ms, bank_settings

        kept = {k: v for k, v in zip(KEEP, r.mget(KEEP)) if v is not None}
        for prefix in KEY_PREFIXES:
            keys = list(r.scan_iter(match=prefix + "*", count=1000))
            for i in range(0, len(keys), 1000):
                r.delete(*keys[i:i + 1000])
        r.delete(*STREAMS)
        r.mset(dict(CFG_DEFAULTS, **kept))
        slots = slots_for(base_rps)
        bank = bank_settings(self.system, base_rps, slots, bank_service_ms(base_rps, slots),
                             float(CFG_DEFAULTS["cfg:standin_low_limit"]))
        r.mset({"bank:cfg:slots": str(bank["slots"]), "bank:cfg:service_ms": str(bank["service_ms"]),
                "bank:cfg:deadline_ms": "100", "bank:cfg:fail_mode": bank["fail_mode"],
                "bank:cfg:fallback_limit": f"{bank['fallback_limit']:g}"})
        if bank["fp_limit_rps"] is not None:
            r.set("cfg:fp_limit_rps", f"{bank['fp_limit_rps']:g}")
        return {"bank_config": bank, "loaded": load(r, self.data)}

    def start(self, name: str, args: list, **extra) -> None:
        log = open(os.path.join(self.logs, f"{name}.log"), "w")
        p = subprocess.Popen([sys.executable] + args, cwd=REPO, env=self.env(**extra), stdout=log,
                             stderr=subprocess.STDOUT)
        with open(os.path.join(self.run_dir, f"{name}.pid"), "w") as fh:
            fh.write(str(p.pid))
        self.procs.append((name, p, log.name))

    def wait(self, name: str, check, timeout: float = 40.0) -> None:
        proc = next(p for n, p, _ in self.procs if n == name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if check():
                return
            time.sleep(0.25)
        logfile = next(f for n, _, f in self.procs if n == name)
        tail = open(logfile, encoding="utf-8", errors="replace").read()[-1500:]
        self.stop()
        raise SystemExit(f"devstack: {name} did not become healthy. Last lines of {logfile}:\n{tail}")

    def http_ok(self, url: str):
        import httpx

        def check():
            try:
                return httpx.get(url, timeout=0.5).status_code == 200
            except httpx.HTTPError:
                return False

        return check

    def up(self) -> None:
        for base in (8000, 8080, 8500, 9000) + ((8001,) if self.system == "shield" else ()):
            if _port_open(self.port(base)):
                raise SystemExit(f"devstack: port {self.port(base)} is already in use; stop that process or pass "
                                 "--offset")
        self.start("bank", ["-m", "bank"])
        self.wait("bank", self.http_ok(f"http://127.0.0.1:{self.port(9000)}/bank/health"))
        if self.system == "shield":
            self.start("decision-8001", ["-m", "decision"], RF_MODE="shield", RF_REPLICA="8001",
                       RF_ENGINE_URL=f"http://127.0.0.1:{self.port(9000)}")
            self.start("ledger-writer", ["-m", "ledger.writer"])
            self.start("sentinel", ["-m", "sentinel.worker"])
            self.wait("decision-8001", self.http_ok(f"http://127.0.0.1:{self.port(8001)}/health"))
        self.start("edge-standin", ["-m", "devstack", "edge", "--mode", SYSTEMS[self.system]])
        self.wait("edge-standin", lambda: _port_open(self.port(8080)))
        self.start("perimeter", ["-m", "perimeter"])
        self.wait("perimeter", self.http_ok(f"http://127.0.0.1:{self.port(8000)}/perimeter/health"))
        self.start("console", ["-m", "console"], RF_ENGINE_URL=f"http://127.0.0.1:{self.port(9000)}")
        self.wait("console", self.http_ok(f"http://127.0.0.1:{self.port(8500)}/"))

    def stop(self) -> None:
        for _, p, _ in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for _, p, _ in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []
        if self.fake_valkey is not None:
            self.fake_valkey.terminate()
            self.fake_valkey.wait(timeout=5)
            self.fake_valkey = None


def _built(data: str) -> dict:
    need = {"accounts": os.path.join(data, "bank", "accounts.csv"),
            "replay": os.path.join(data, "replay", "ibm_cards_test.csv"),
            "build": os.path.join(data, "vault", "BUILD.json")}
    missing = [k for k, p in need.items() if not os.path.exists(p)]
    if missing:
        raise SystemExit(f"devstack: the vault isn't built in {data} (missing {', '.join(missing)}). Copy the IBM "
                         "sample files into <data>/vault/, then run `python -m vault seal` and `python -m vault build`.")
    return need


def _archive(data: str, run_id: str) -> None:
    """Move the previous run's ledger and postings aside, as reset.sh does. Nothing is deleted."""
    dest = os.path.join(data, "archive", run_id)
    for rel in ("ledger/ledger.jsonl", "bank/postings.jsonl"):
        src = os.path.join(data, rel)
        if os.path.exists(src):
            os.makedirs(dest, exist_ok=True)
            shutil.move(src, os.path.join(dest, os.path.basename(src)))


def _jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _collect(stack: Stack, r, run_id: str, lab: dict, truth_path: str) -> dict:
    import httpx

    from eval.scenarios import metrics

    perimeter = httpx.get(f"http://127.0.0.1:{stack.port(8000)}/perimeter/stats", timeout=2).json()
    books = httpx.get(f"http://127.0.0.1:{stack.port(9000)}/bank/books", timeout=2).json()
    ledger_path = os.path.join(stack.data, "ledger", "ledger.jsonl")
    ledger_ok = None
    if stack.system == "shield":
        from ledger.verify import verify_file

        ledger_ok = bool(verify_file(ledger_path)["ok"])
    m = metrics.compute(
        truth=_jsonl(truth_path), postings=_jsonl(os.path.join(stack.data, "bank", "postings.jsonl")),
        bank_log=[f for _, f in r.xrange("bank.log")], decisions=[f for _, f in r.xrange("decisions")],
        threat=[f for _, f in r.xrange("threat")], ledger_records=_jsonl(ledger_path), ledger_ok=ledger_ok, cpu_s={},
        system=stack.system, window_start=lab.get("start_ts", 0) + lab.get("warmup_s", 0))
    keys = ("legit_sent", "legit_completed", "legit_held", "legit_blocked", "fraud_count_sent", "fraud_value_sent",
            "fraud_value_through", "fail_open_value", "attack_sent", "attack_shed", "p50_ms", "p99_ms",
            "deadline_miss_rate", "bank_engine_p99_ms", "stand_in_count", "ledger_ok", "reconcile_mismatch")
    return {"run_id": run_id, "system": stack.system, "entry": "perimeter",
            "note": "devstack laptop run: a working check of the chain, not a LinuxONE measurement",
            "lab": {k: lab.get(k) for k in ("sent", "valid", "achieved_ratio", "by_kind", "statuses")},
            "perimeter": perimeter, "bank_books": books, "metrics": {k: m.get(k) for k in keys},
            "scores": m.get("scores"), "truth_file": truth_path}


def demo(args) -> int:
    data = os.path.abspath(args.data)
    files = _built(data)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    run_id = f"dev-{os.path.basename(args.scenario).replace('.json', '').lower()}-{args.system}-{stamp}"
    stack = Stack(args.system, data, args.offset, args.valkey)
    r = stack.ensure_valkey(args.allow_reset)
    try:
        _archive(data, run_id)
        setup = stack.reset(r, args.base_rps)
        stack.up()
        truth_dir = os.path.join(data, "truth")
        cmd = [sys.executable, "-m", "lab", "run", "--scenario", args.scenario, "--run-id", run_id, "--system",
               args.system, "--base-rps", f"{args.base_rps:g}", "--duration-s", f"{args.seconds:g}", "--replay",
               files["replay"], "--truth-dir", truth_dir, "--entry", "perimeter"]
        print("+", " ".join(cmd[1:]), flush=True)
        done = subprocess.run(cmd, cwd=REPO, env=stack.env(), capture_output=True, text=True,
                              timeout=args.seconds + 180)
        try:
            lab = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            print(done.stderr[-1500:], file=sys.stderr)
            raise SystemExit(f"devstack: the lab failed (exit {done.returncode})")
        time.sleep(5)   # settles
        out = _collect(stack, r, run_id, lab, os.path.join(truth_dir, f"{run_id}.jsonl"))
        out["setup"] = setup
        dest = os.path.join(data, "results", "dev")
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, f"{run_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
        print(json.dumps(out, indent=1))
        return 0 if done.returncode == 0 else 2
    finally:
        stack.stop()


def up(args) -> int:
    _built(os.path.abspath(args.data))
    stack = Stack(args.system, args.data, args.offset, args.valkey)
    r = stack.ensure_valkey(args.allow_reset)
    try:
        print(json.dumps(stack.reset(r, args.base_rps), indent=1))
        stack.up()
        print(f"devstack: {args.system} is up. Send traffic to http://127.0.0.1:{stack.port(8000)} (the perimeter); "
              f"console at http://127.0.0.1:{stack.port(8500)}; logs in {stack.logs}. Ctrl+C stops everything.")
        while all(p.poll() is None for _, p, _ in stack.procs):
            time.sleep(1)
        print("devstack: a process exited; stopping", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        stack.stop()


def edge(args) -> int:
    import redis.asyncio as aioredis
    import uvicorn

    from .edge import create_app

    offset = int(os.environ.get("RF_PORT_OFFSET", "0"))
    r = aioredis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    uvicorn.run(create_app(args.mode, redis=r, offset=offset), host="127.0.0.1", port=8080 + offset,
                log_level="warning", access_log=False)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="devstack")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("up", "demo"):
        p = sub.add_parser(name)
        p.add_argument("--system", choices=tuple(SYSTEMS), default="shield")
        p.add_argument("--data", default=os.environ.get("RF_DATA", os.path.join(REPO, "data")))
        p.add_argument("--offset", type=int, default=int(os.environ.get("RF_PORT_OFFSET", "0")))
        p.add_argument("--valkey", default=os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"))
        p.add_argument("--base-rps", type=float, default=40.0, help="R; the bank's capacity is set to 4R")
        p.add_argument("--allow-reset", action="store_true",
                       help="allow clearing RiskFabric's keys in a real Valkey (never the shared stack's)")
        if name == "demo":
            p.add_argument("--scenario", default="S1")
            p.add_argument("--seconds", type=float, default=90.0)
    e = sub.add_parser("edge")
    e.add_argument("--mode", choices=("riskfabric", "statusquo", "realistic"), required=True)
    args = ap.parse_args(argv)
    if args.cmd in ("up", "demo"):
        args.data = os.path.abspath(args.data)
        os.environ["RF_PORT_OFFSET"] = str(args.offset)
    return {"up": up, "demo": demo, "edge": edge}[args.cmd](args)


if __name__ == "__main__":
    sys.path.insert(0, REPO)
    sys.exit(main())
