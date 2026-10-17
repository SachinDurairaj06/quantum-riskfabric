"""Sentinel's decision logic, free of I/O so it can be tested window by window (prd/04 section 4.2)."""
from __future__ import annotations

import collections
import json
import statistics
from typing import Optional

from . import rules as R

ACTIONS = ("throttle", "stepup", "block")


def _clip(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


class KeyWindow:
    __slots__ = ("count", "ips", "asns", "known", "shed")

    def __init__(self):
        self.count, self.ips, self.asns, self.known, self.shed = 0, set(), collections.Counter(), 0, 0


class Cluster:
    def __init__(self, cid: str, key_type: str, key: str, protected: bool):
        self.id, self.key_type, self.key, self.protected = cid, key_type, key, protected
        self.bad, self.good, self.action = 0, 0, None
        self.until, self.score, self.rps, self.share, self.decline = 0.0, 0.0, 0.0, 0.0, 0.0

    def to_threat(self) -> dict:
        return {"id": self.id, "key_type": self.key_type, "key": self.key, "rps": round(self.rps, 2),
                "share": round(self.share, 4), "decline_rate": round(self.decline, 4), "risk": round(_clip(self.score), 3),
                "action": self.action, "until": round(self.until, 3)}


class Sentinel:
    def __init__(self):
        self.window: dict = {}                       # key -> KeyWindow for the open window
        self.window_total = 0
        self.window_known = 0
        self.window_fps: set = set()
        self.baseline: dict = {}                     # key -> EWMA share
        self.baseline_known: dict = {}               # fp key -> EWMA known-client share in NORMAL windows
        self.baseline_total: Optional[float] = None
        self.normal_windows = 0
        self.req_keys: collections.OrderedDict = collections.OrderedDict()   # req_id -> (fp_key, fpasn_key)
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=R.HISTORY_WINDOWS))  # key -> per-window [vreq, vok, dec, hold]
        self.cur = collections.defaultdict(lambda: [0, 0, 0, 0])
        self.last_seen: dict = {}                    # ip -> last ts
        self.gaps = collections.defaultdict(lambda: collections.deque(maxlen=5))
        self.ip_key: dict = {}                       # ip -> (fp_key, fpasn_key)
        self.clusters: dict = {}                     # key -> Cluster
        self.next_id = 1
        self.seq = 0

    # ---------- ingest ----------
    def edge(self, f: dict) -> None:
        fp, asn, ip = f.get("fp") or "-", f.get("asn") or "-", f.get("ip") or "-"
        keys = (f"fp:{fp}", f"fp_asn:{fp}|{asn}")
        path = f.get("path") or "/pay"
        status = f.get("status") or ""
        if path.startswith("/verify"):
            if status == "200":
                for k in keys:
                    self.cur[k][1] += 1
            return
        req_id = f.get("req_id")
        if req_id:
            self.req_keys[req_id] = keys
            while len(self.req_keys) > 200000:
                self.req_keys.popitem(last=False)
        known = f.get("known") == "1"
        self.window_total += 1
        self.window_known += known
        self.window_fps.add(fp)
        for k in keys:
            kw = self.window.get(k)
            if kw is None:
                kw = self.window[k] = KeyWindow()
            kw.count += 1
            kw.ips.add(ip)
            kw.asns[asn] += 1
            kw.known += known
            kw.shed += status in ("403", "429")
        try:
            ts = float(f.get("ts"))
        except (TypeError, ValueError):
            return
        prev = self.last_seen.get(ip)
        if prev is not None and ts > prev:
            self.gaps[ip].append(ts - prev)
        self.last_seen[ip] = ts
        self.ip_key[ip] = keys

    def decision(self, f: dict) -> None:
        keys = self.req_keys.get(f.get("req_id"))
        if not keys:
            return
        action = f.get("action")
        for k in keys:
            c = self.cur[k]
            c[2] += 1
            c[0] += action == "VERIFY"
            c[3] += action == "HOLD"

    # ---------- per key ----------
    def _rolling(self, key: str) -> tuple:
        rows = list(self.hist[key]) + [self.cur.get(key, [0, 0, 0, 0])]
        return tuple(sum(r[i] for r in rows) for i in range(4))

    def _regularity(self, key: str) -> Optional[float]:
        cvs = []
        for ip in self.window[key].ips:
            g = self.gaps.get(ip)
            if g is not None and len(g) >= 3:
                mean = statistics.fmean(g)
                if mean > 0:
                    cvs.append(statistics.pstdev(g) / mean)
        return statistics.median(cvs) if len(cvs) >= R.REGULAR_MIN_IPS else None

    def score(self, key: str) -> dict:
        kw = self.window[key]
        share = kw.count / self.window_total if self.window_total else 0.0
        jump = share - self.baseline.get(key, 0.0)
        known_share = kw.known / kw.count if kw.count else 0.0
        vreq, vok, dec, hold = self._rolling(key)
        completion = (vok / vreq) if vreq >= R.VERIFY_MIN else None
        reg = self._regularity(key)
        top_asn = max(kw.asns.values()) / kw.count if kw.count else 0.0
        s = (R.W_JUMP * _clip(jump / R.JUMP_FULL) + R.W_UNKNOWN * (1.0 - known_share)
             + R.W_VERIFY * (1.0 - completion if completion is not None else 0.5)
             + R.W_REGULAR * (1.0 if reg is not None and reg < R.REGULAR_CV else 0.0)
             + R.W_ASN * _clip((top_asn - R.ASN_FLOOR) / R.ASN_SPAN))
        return {"score": s, "share": share, "known_share": known_share, "rps": kw.count / R.WINDOW_S,
                "decline": (hold / dec) if dec else 0.0, "candidate": kw.count >= R.MIN_COUNT and share >= R.MIN_SHARE}

    # ---------- window close ----------
    def close(self, window_ts: float) -> dict:
        """Close the open window and return the `threat` entry for it (CONTRACTS section 5)."""
        scored = {k: self.score(k) for k in self.window}
        # Protection uses the fingerprint's normal customer mix, so an attack that borrows it can't strip it.
        fp_protected = {k: max(v["known_share"], self.baseline_known.get(k, 0.0)) >= R.PROTECTED_KNOWN_SHARE
                        for k, v in scored.items() if k.startswith("fp:")}
        bad_now: dict = {}
        for k, v in scored.items():
            if not v["candidate"] or v["score"] < R.BAD_SCORE:
                continue
            if k.startswith("fp:") and fp_protected.get(k):
                continue  # never act on the bank app's fingerprint as a whole
            if k.startswith("fp_asn:"):
                parent = "fp:" + k[len("fp_asn:"):].split("|", 1)[0]
                if parent in scored and scored[parent]["candidate"] and scored[parent]["score"] >= R.BAD_SCORE \
                        and not fp_protected.get(parent):
                    continue  # the whole fingerprint is already acted on
            bad_now[k] = v

        for k, v in bad_now.items():
            c = self.clusters.get(k)
            if c is None:
                key_type, key = k.split(":", 1)
                parent = "fp:" + key.split("|", 1)[0] if key_type == "fp_asn" else k
                c = self.clusters[k] = Cluster(f"c-{self.next_id}", key_type, key, bool(fp_protected.get(parent)))
                self.next_id += 1
            c.bad, c.good = c.bad + 1, 0
            c.score, c.rps, c.share, c.decline = v["score"], v["rps"], v["share"], v["decline"]
        for k, c in list(self.clusters.items()):
            if k in bad_now:
                continue
            c.good += 1
            c.bad = c.bad if c.action else 0
            if k in scored:
                c.rps, c.share, c.score = scored[k]["rps"], scored[k]["share"], scored[k]["score"]
            if c.good >= R.RELEASE_GOOD or (c.action is None and c.good >= 1):
                del self.clusters[k]

        for c in self.clusters.values():
            if c.bad >= R.ENTER_BAD + R.STEPUP_AFTER + R.BLOCK_AFTER and not c.protected:
                c.action = "block"
            elif c.bad >= R.ENTER_BAD + R.STEPUP_AFTER:
                c.action = "stepup"
            elif c.bad >= R.ENTER_BAD:
                c.action = c.action or "throttle"
            if c.protected and c.action == "block":
                c.action = "stepup"
            if c.action:
                c.until = window_ts + R.UNTIL_S

        active = [c for c in self.clusters.values() if c.action]
        level = "NORMAL"
        if any(c.share >= R.COORDINATED_SHARE for c in active):
            level = "COORDINATED"
        elif (self.baseline_total and self.window_total > R.SURGE_FACTOR * self.baseline_total
              and len(self.window_fps) >= R.SURGE_MIN_FPS
              and (self.window_known / self.window_total if self.window_total else 0) >= R.SURGE_MIN_KNOWN):
            level = "SURGE"

        if level != "COORDINATED":
            self.normal_windows += 1
            alpha = max(1.0 / R.BASELINE_WINDOWS, 1.0 / self.normal_windows)
            seen = set(scored)
            for k in seen | set(self.baseline):
                share = scored[k]["share"] if k in scored else 0.0
                self.baseline[k] = (1 - alpha) * self.baseline.get(k, 0.0) + alpha * share
            for k, v in scored.items():
                # learn a fingerprint's normal mix only from windows where it looks normal itself, so a building
                # attack that borrows the fingerprint can't wear its protection down before it is detected
                if (k.startswith("fp:") and self.window[k].count >= R.LEARN_MIN_COUNT and k not in self.clusters
                        and v["score"] < R.BAD_SCORE):
                    self.baseline_known[k] = (1 - alpha) * self.baseline_known.get(k, v["known_share"]) + alpha * v["known_share"]
            if level == "NORMAL":
                bt = self.baseline_total
                self.baseline_total = self.window_total if bt is None else (1 - alpha) * bt + alpha * self.window_total
            self.baseline = {k: v for k, v in self.baseline.items() if v > 1e-4}

        self.seq += 1
        entry = {"ts": window_ts, "seq": self.seq, "level": level,
                 "clusters": [c.to_threat() for c in sorted(active, key=lambda c: -c.share)]}
        self._roll()
        return entry

    def _roll(self) -> None:
        for k in set(self.hist) | set(self.cur):
            self.hist[k].append(self.cur.get(k, [0, 0, 0, 0]))
        self.cur.clear()
        for k in [k for k, h in self.hist.items() if not any(any(r) for r in h)]:
            del self.hist[k]
        self.window, self.window_total, self.window_known, self.window_fps = {}, 0, 0, set()
        if len(self.last_seen) > 50000:
            cutoff = sorted(self.last_seen.values())[len(self.last_seen) // 2]
            for ip in [ip for ip, t in self.last_seen.items() if t < cutoff]:
                self.last_seen.pop(ip, None)
                self.gaps.pop(ip, None)
                self.ip_key.pop(ip, None)


def threat_fields(entry: dict) -> dict:
    return {"ts": f"{entry['ts']:.3f}", "seq": str(entry["seq"]), "level": entry["level"],
            "clusters": json.dumps(entry["clusters"], separators=(",", ":"))}
