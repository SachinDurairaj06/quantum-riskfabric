"""Environment settings for the decision service (CONTRACTS section 2)."""
from __future__ import annotations

import os
from dataclasses import dataclass

MODES = ("standalone", "shield")
VARIANTS = ("riskfabric", "submitted_design")
REPLICAS = ("8001", "8002")


@dataclass(frozen=True)
class Config:
    valkey_url: str
    deadline_ms: int
    mode: str
    variant: str
    replica: str
    engine_url: str
    data_dir: str
    port_offset: int

    @property
    def role(self) -> str:
        return "primary" if self.replica == "8001" else "standby"

    @property
    def model_dir(self) -> str:
        return os.path.join(self.data_dir, "models")


def _choice(name: str, default: str, allowed: tuple) -> str:
    value = os.environ.get(name, default)
    if value not in allowed:
        raise ValueError(f"{name}={value!r}; allowed: {', '.join(allowed)}")
    return value


def load() -> Config:
    offset = int(os.environ.get("RF_PORT_OFFSET", "0"))
    return Config(
        valkey_url=os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"),
        deadline_ms=int(os.environ.get("RF_DEADLINE_MS", "100")),
        mode=_choice("RF_MODE", "standalone", MODES),
        variant=_choice("RF_VARIANT", "riskfabric", VARIANTS),
        replica=_choice("RF_REPLICA", "8001", REPLICAS),
        engine_url=os.environ.get("RF_ENGINE_URL", f"http://127.0.0.1:{9000 + offset}"),
        data_dir=os.environ.get("RF_DATA", "/srv/riskfabric/data"),
        port_offset=offset,
    )
