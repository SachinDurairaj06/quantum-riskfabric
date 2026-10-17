"""Synthetic client identities (prd/04 section 3.1). Header values only: nothing is spoofed on the network.

Fingerprints for every population look alike (`fp-` plus 8 hex digits), so Sentinel can't cheat by reading names.
IPs come from the benchmarking range 198.18.0.0/15; networks from the private ASN range.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Optional

N_FINGERPRINTS = 50
APP_SHARE = 0.40          # the bank's own app
N_NETWORKS = 30
KNOWN_SHARE = 0.80


@dataclass(frozen=True)
class Identity:
    ip: str
    fp: str
    asn: int
    known: bool
    acct: Optional[str] = None


def _fp(rng: random.Random) -> str:
    return f"fp-{rng.getrandbits(32):08x}"


def _ip(rng: random.Random) -> str:
    return f"198.{18 + rng.getrandbits(1)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"


class Population:
    def __init__(self, seed: int):
        self.seed = seed
        rng = random.Random(seed)
        self.fingerprints = [_fp(rng) for _ in range(N_FINGERPRINTS)]
        rest = [1.0 / (i + 1) ** 1.1 for i in range(1, N_FINGERPRINTS)]
        scale = (1.0 - APP_SHARE) / sum(rest)
        self.fp_weights = [APP_SHARE] + [w * scale for w in rest]
        self.networks = [64512 + i for i in range(N_NETWORKS)]
        self.net_weights = [1.0 / (i + 1) ** 0.8 for i in range(N_NETWORKS)]
        self._rng = rng

    @property
    def app_fingerprint(self) -> str:
        return self.fingerprints[0]

    def customer(self, acct: str) -> Identity:
        """The same account always gets the same client."""
        digest = hashlib.sha256(f"{self.seed}:{acct}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        return Identity(ip=_ip(rng), fp=rng.choices(self.fingerprints, self.fp_weights)[0],
                        asn=rng.choices(self.networks, self.net_weights)[0], known=rng.random() < KNOWN_SHARE, acct=acct)

    def bots(self, n: int, tool_fps: int, networks: int, use_app_fp: bool = False) -> list:
        """Abusive identities: one tool (1 or 2 fingerprints), a few networks, each IP unique."""
        rng = random.Random(self.seed * 7919 + 1)
        fps = [self.app_fingerprint] if use_app_fp else [_fp(rng) for _ in range(tool_fps)]
        nets = [64900 + rng.randrange(400) for _ in range(networks)]
        seen, out = set(), []
        while len(out) < n:
            ip = _ip(rng)
            if ip in seen:
                continue
            seen.add(ip)
            out.append(Identity(ip=ip, fp=fps[len(out) % len(fps)], asn=nets[len(out) % len(nets)], known=False,
                                acct=f"lab-{rng.getrandbits(32):08x}"))
        return out

    def card_testers(self, accounts: list, n: int = 5) -> list:
        """A handful of scripted identities using a small group of real (stolen) registry accounts."""
        rng = random.Random(self.seed * 104729 + 3)
        fp = _fp(rng)
        return [Identity(ip=_ip(rng), fp=fp, asn=64900 + rng.randrange(400), known=False,
                         acct=accounts[i % len(accounts)] if accounts else None) for i in range(n)]
