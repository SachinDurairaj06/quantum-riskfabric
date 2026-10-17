"""python -m eval.scenarios.runner   (prd/02 sections 6 and 7; C's scenario runner)

  run       one (scenario, system, repeat):  --scenario S4 --system shield --repeat 1 --base-rps 200
            --bank-fail-mode closed runs the fail-closed sensitivity check (prd/02 section 6, rule 8)
  all       the MUST matrix (prd/02 section 3: 36 runs, 3 repeats, system order alternating); --should adds SHOULD cells
  capacity  the S3 mix on shield, CPU per request per tier, cores at 100,000 requests/s with 80% attack
  dry       print the plan of `all`, the fixed rules and a rough duration, without running anything

It uses D's deploy/start.sh, deploy/stop.sh and deploy/reset.sh, B's lab and E's ledger verifier. It tails the
streams during the run (they are capped at 100,000 entries) into $RF_DATA/results/raw/<run_id>/, copies the ledger
and the bank's postings there, computes the metrics and writes eval/results/<scenario>__<system>__<stamp>.json.
Result files are never deleted; a run that went wrong is written with valid=false and its reason.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from . import metrics
from .summary import RESULTS, REPO, SYSTEMS

DATA = os.environ.get("RF_DATA", "/srv/riskfabric/data")
RUN = os.environ.get("RF_RUN", "/srv/riskfabric/run")
STREAMS = ("edge.log", "decisions", "bank.log", "threat")
BANK_SYSTEMS = metrics.BANK_SYSTEMS
# prd/02 section 3. MUST cells run 3 times each (36 runs); SHOULD cells once.
MUST_CELLS = {
    "S1": ("shield", "bank_realistic", "bank_only"),
    "S2": ("shield",),
    "S3": ("shield",),
    "S4": ("shield", "bank_realistic", "bank_only"),
    "S5": ("shield", "bank_realistic"),
    "S6": ("shield", "bank_realistic"),
}
SHOULD = ([(s, "bank_only") for s in ("S2", "S3", "S5", "S6")] + [(s, "bank_realistic") for s in ("S2", "S3")]
          + [(s, "standalone") for s in ("S1", "S2", "S3", "S4")]
          + [("S4b", s) for s in ("bank_only", "bank_realistic", "shield")])
EST_MINUTES = {"bank_only": 4.0, "bank_realistic": 4.0}  # rough per run, incl. reset and start; others 5.5
TIER_OF_PID = (("haproxy", "edge"), ("decision", "decision"), ("sentinel", "sentinel"), ("bank", "bank"), ("quantum", "quantum"))
FAULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faults.json")


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")


def _sh(cmd: list, timeout: int = 600) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)


def _redis():
    import redis

    return redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)


class Tailer(threading.Thread):
    """Copies new stream entries to JSON lines while the run is going."""

    def __init__(self, r, out_dir: str):
        super().__init__(daemon=True)
        self.r, self.out_dir, self.stop = r, out_dir, threading.Event()
        self.ids = {}
        for s in STREAMS:
            newest = r.xrevrange(s, count=1)
            self.ids[s] = newest[0][0] if newest else "0-0"
        os.makedirs(out_dir, exist_ok=True)
        self.files = {s: open(os.path.join(out_dir, f"{s}.jsonl"), "a", encoding="utf-8") for s in STREAMS}

    def run(self) -> None:
        while not self.stop.is_set():
            self.drain(block_ms=500)
        self.drain(block_ms=1)
        for fh in self.files.values():
            fh.close()

    def drain(self, block_ms: int) -> None:
        resp = self.r.xread(self.ids, count=10000, block=block_ms)
        for stream, entries in resp or []:
            fh = self.files[stream]
            for entry_id, fields in entries:
                self.ids[stream] = entry_id
                fh.write(json.dumps(fields, separators=(",", ":")) + "\n")


def _read_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cpu_seconds() -> dict:
    """utime + stime of the stack's processes by tier, from D's pid files in $RF_RUN (Linux /proc only)."""
    out: dict = {}
    if not os.path.isdir(RUN) or not os.path.isdir("/proc"):
        return out
    tick = os.sysconf("SC_CLK_TCK")
    for name in os.listdir(RUN):
        if not name.endswith(".pid"):
            continue
        tier = next((t for key, t in TIER_OF_PID if key in name), None)
        if tier is None:
            continue
        try:
            pid = int(open(os.path.join(RUN, name)).read().strip())
            fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            out[tier] = out.get(tier, 0.0) + (int(fields[11]) + int(fields[12])) / tick
        except (OSError, ValueError, IndexError):
            continue
    return out


def nnpa_present() -> bool:
    try:
        return "nnpa" in open("/proc/cpuinfo").read()
    except OSError:
        return False


def _faults():
    if os.path.exists(FAULTS_FILE):
        with open(FAULTS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def schedule_faults(scenario_faults: list, r, base_service_ms: float, start: float, notes: list) -> list:
    """Starts timers for the scenario's faults. bank_brownout is C's (bank:cfg:service_ms); the others use the
    commands in eval/scenarios/faults.json (D owns tc on lo; stress-ng is a standard tool)."""
    cmds = _faults()
    timers = []

    def at(delay, fn):
        t = threading.Timer(max(0.0, start + delay - time.time()), fn)
        t.daemon = True
        t.start()
        timers.append(t)

    for f in scenario_faults:
        kind, dur = f["type"], f["stop_s"] - f["start_s"]
        if kind == "bank_brownout":
            slow = str(base_service_ms * f["factor"])
            at(f["start_s"], lambda slow=slow: r.set("bank:cfg:service_ms", slow))
            at(f["stop_s"], lambda: r.set("bank:cfg:service_ms", str(base_service_ms)))
            continue
        template = cmds.get(kind)
        if not template:
            notes.append(f"fault {kind} not applied: no command in eval/scenarios/faults.json (ask D)")
            continue
        cmd = [part.format(duration_s=int(dur), **f) for part in template]
        at(f["start_s"], lambda cmd=cmd: subprocess.Popen(cmd, cwd=REPO))
    return timers


def fp_limit_rps(base_rps: float) -> float:
    """The realistic bank's per-fingerprint limit (prd/02 section 6): twice the bank app fingerprint's payday rate.
    The app carries about 40% of customers and S2 runs at 3R, so the app's payday rate is 1.2R and the limit 2.4R."""
    return round(2.4 * base_rps, 3)


def bank_settings(system: str, base_rps: float, slots: int, service_ms: float, fallback_limit: float,
                  fail_mode: Optional[str] = None) -> dict:
    """The bank's settings for one run. The engine (rules, model, capacity) is the same for every system; only the
    channel's timeout fallback and the realistic bank's edge limit depend on the system."""
    return {"slots": slots, "service_ms": service_ms,
            "fail_mode": fail_mode or ("bounded" if system == "bank_realistic" else "open"),
            "fallback_limit": fallback_limit,
            "fp_limit_rps": fp_limit_rps(base_rps) if system == "bank_realistic" else None}


def run_one(scenario: str, system: str, repeat: int, base_rps: float, service_ms: float, slots: int,
            fail_mode: Optional[str] = None) -> dict:
    run_id = f"{scenario.lower()}-{system}-{repeat:02d}" + (f"-{fail_mode}" if fail_mode else "")
    raw = os.path.join(DATA, "results", "raw", run_id)
    command = f"python -m eval.scenarios.runner run --scenario {scenario} --system {system} --repeat {repeat} --base-rps {base_rps:g}"
    if fail_mode:
        command += f" --bank-fail-mode {fail_mode}"
    notes: list = []
    r = _redis()
    _sh(["deploy/stop.sh"])
    reset = _sh(["deploy/reset.sh", "--yes"])
    if reset.returncode != 0:
        notes.append(f"reset failed: {reset.stderr.strip()[:200]}")
    low = r.get("cfg:standin_low_limit")
    bank = bank_settings(system, base_rps, slots, service_ms, float(low) if low else 10000.0, fail_mode)
    r.set("bank:cfg:slots", str(slots))
    r.set("bank:cfg:service_ms", str(service_ms))
    r.set("bank:cfg:fail_mode", bank["fail_mode"])
    r.set("bank:cfg:fallback_limit", f"{bank['fallback_limit']:g}")
    if bank["fp_limit_rps"] is not None:
        r.set("cfg:fp_limit_rps", f"{bank['fp_limit_rps']:g}")
    start = _sh(["deploy/start.sh", "--system", system])
    if start.returncode != 0:
        notes.append(f"start failed: {start.stderr.strip()[:200]}")
    tail = Tailer(r, raw)
    tail.start()
    cpu0 = cpu_seconds()
    spec = json.load(open(os.path.join(REPO, "lab", "scenarios", f"{scenario}.json"), encoding="utf-8"))
    t_start = time.time()
    timers = schedule_faults(spec.get("faults", []), r, service_ms, t_start, notes)
    lab = _sh([sys.executable, "-m", "lab", "run", "--scenario", scenario, "--run-id", run_id, "--system", system,
               "--base-rps", f"{base_rps:g}"], timeout=int(spec["duration_s"]) + 300)
    for t in timers:
        t.cancel()
    r.set("bank:cfg:service_ms", str(service_ms))
    time.sleep(5)  # settles
    cpu1 = cpu_seconds()
    tail.stop.set()
    tail.join(timeout=10)
    try:
        lab_summary = json.loads(lab.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        lab_summary = {"valid": False, "start_ts": t_start, "warmup_s": spec.get("warmup_s", 30)}
        notes.append(f"lab failed: {lab.stderr.strip()[:200]}")
    if lab.returncode == 2:
        notes.append(f"lab achieved {lab_summary.get('achieved_ratio')} of the offered rate (below 0.95)")
    ledger_path = os.path.join(DATA, "ledger", "ledger.jsonl")
    postings_path = os.path.join(DATA, "bank", "postings.jsonl")
    for src in (ledger_path, postings_path):
        if os.path.exists(src):
            shutil.copy2(src, raw)
    ledger_ok = None
    if system not in BANK_SYSTEMS:
        from ledger.verify import verify_file

        ledger_ok = bool(verify_file(ledger_path)["ok"])
    threat = _read_jsonl(os.path.join(raw, "threat.jsonl"))
    for e in threat:
        try:
            e["clusters"] = json.loads(e.get("clusters") or "[]")
        except ValueError:
            e["clusters"] = []
    policy = {}
    pol_path = os.path.join(REPO, "scoring", "policy.json")
    if os.path.exists(pol_path):
        policy = json.load(open(pol_path, encoding="utf-8"))
    m = metrics.compute(
        truth=_read_jsonl(os.path.join(DATA, "truth", f"{run_id}.jsonl")),
        postings=_read_jsonl(os.path.join(raw, "postings.jsonl")),
        bank_log=_read_jsonl(os.path.join(raw, "bank.log.jsonl")),
        decisions=_read_jsonl(os.path.join(raw, "decisions.jsonl")),
        threat=threat, ledger_records=_read_jsonl(os.path.join(raw, "ledger.jsonl")), ledger_ok=ledger_ok,
        cpu_s={k: cpu1.get(k, 0) - cpu0.get(k, 0) for k in cpu1} if cpu1 else {},
        system=system, window_start=lab_summary.get("start_ts", t_start) + lab_summary.get("warmup_s", 30),
        deadline_ms=float(os.environ.get("RF_DEADLINE_MS", "100")), hold_at=float(policy.get("hold_at", 0.8)))
    valid = bool(lab_summary.get("valid")) and not notes
    result = {"contract": 2, "scenario": scenario, "system": system, "run_id": run_id, "repeat": repeat,
              "ts": time.time(), "duration_s": spec["duration_s"] - spec.get("warmup_s", 30),
              "commit": _git_commit(), "host": platform.node(), "arch": platform.machine(), "nnpa": nnpa_present(),
              "command": command, "valid": valid, "invalid_reason": "; ".join(notes) or None,
              "rate": {"offered_legit_rps": base_rps, "offered_attack_rps": _attack_rps(spec, base_rps),
                       "achieved_total_rps": round(lab_summary.get("sent", 0) / max(spec["duration_s"], 1), 2)},
              "bank_config": bank, **m}
    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, f"{scenario}__{system}__{_stamp()}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    print(f"{run_id}: valid={valid} -> {out}", flush=True)
    return result


def _attack_rps(spec: dict, base_rps: float) -> float:
    return sum(p.get("rate_x", 0) * base_rps for p in spec["populations"] if p["kind"] in ("bot", "cardtest"))


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip() or "uncommitted"
    except (OSError, subprocess.TimeoutExpired):
        return "uncommitted"


def plan(should: bool, repeats: int = 3) -> list:
    """MUST cells 3 times each, reversing the system order every other repeat; SHOULD cells once."""
    out = []
    for rep in range(1, repeats + 1):
        for scen, systems in MUST_CELLS.items():
            order = systems if rep % 2 else tuple(reversed(systems))
            out += [(scen, s, rep) for s in order]
    if should:
        out += [(scen, s, 1) for scen, s in SHOULD]
    return out


def bank_service_ms(base_rps: float, slots: int) -> float:
    """The documented 4R rule (prd/03 section 4.3): capacity = slots * 1000 / service_ms = 4R."""
    return round(slots * 1000.0 / (4.0 * base_rps), 4)


def capacity_formula(cpu_us: dict, pass_share: float, rps: float = 100000.0, attack_share: float = 0.8) -> dict:
    c = {k: (cpu_us.get(k) or 0.0) for k in ("edge", "decision", "sentinel", "bank")}
    score_all = rps * (c["edge"] + c["decision"] + c["sentinel"] + c["bank"]) / 1e6
    r_dec = rps * pass_share
    cascade = (rps * c["edge"] + r_dec * c["decision"] + rps * c["sentinel"] + r_dec * c["bank"]) / 1e6
    return {"rps": rps, "attack_share": attack_share, "pass_share": pass_share, "cores_score_all": round(score_all, 3),
            "cores_cascade": round(cascade, 3), "cpu_us_per_req": c}


def run_capacity(base_rps: float, slots: int) -> dict:
    result = run_one("S3", "shield", 99, base_rps, bank_service_ms(base_rps, slots), slots)
    total = (result["attack_sent"] or 0) + (result["legit_sent"] or 0)
    reached = (result["legit_sent"] or 0) + (result["attack_sent"] - result["attack_shed"] if result["attack_sent"] else 0)
    cap = capacity_formula(result["cpu_us_per_req"] or {}, reached / total if total else 1.0)
    cap["command"] = f"python -m eval.scenarios.runner capacity --base-rps {base_rps:g}"
    result.update({"scenario": "CAP", "capacity": cap})
    with open(os.path.join(RESULTS, f"CAP__shield__{_stamp()}.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    return cap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval.scenarios.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "all", "capacity", "dry"):
        p = sub.add_parser(name)
        p.add_argument("--base-rps", type=float, required=True, help="R, set by C at H10")
        p.add_argument("--slots", type=int, default=8)
        if name == "run":
            p.add_argument("--scenario", required=True)
            p.add_argument("--system", required=True, choices=SYSTEMS)
            p.add_argument("--repeat", type=int, default=1)
            p.add_argument("--bank-fail-mode", choices=("open", "closed", "bounded"), default=None,
                           help="override the bank's timeout fallback (sensitivity runs only)")
        if name in ("all", "dry"):
            p.add_argument("--should", action="store_true")
    args = ap.parse_args(argv)
    service_ms = bank_service_ms(args.base_rps, args.slots)
    if args.cmd == "dry":
        items = plan(args.should)
        for item in items:
            print(*item)
        minutes = sum(EST_MINUTES.get(system, 5.5) for _, system, _ in items)
        print(f"{len(items)} runs, roughly {minutes / 60:.1f} h (an estimate, not a measurement)")
        print(f"bank capacity rule: slots {args.slots}, service_ms {service_ms} (capacity {4 * args.base_rps:g}/s)")
        print(f"realistic bank: per-fingerprint limit {fp_limit_rps(args.base_rps):g} requests/s (2.4R); "
              "fallback limit = cfg:standin_low_limit (default 10000)")
        return 0
    if args.cmd == "run":
        run_one(args.scenario, args.system, args.repeat, args.base_rps, service_ms, args.slots, args.bank_fail_mode)
    elif args.cmd == "all":
        for scen, system, rep in plan(args.should):
            run_one(scen, system, rep, args.base_rps, service_ms, args.slots)
    else:
        print(json.dumps(run_capacity(args.base_rps, args.slots)))
    from .summary import main as summary_main

    return summary_main()


if __name__ == "__main__":
    sys.exit(main())
