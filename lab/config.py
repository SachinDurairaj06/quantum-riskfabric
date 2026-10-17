"""Fixed lab settings. The target host is a constant on purpose (prd/04 L-02): there is no flag to change it."""
from __future__ import annotations

import os

TARGET_HOST = "127.0.0.1"
BASE_PORT = 8080
MAX_TOTAL_RPS = 2000          # cap per lab process; D may raise it for the VM (prd/04 section 2)
MAX_IN_FLIGHT = 1000
CLIENT_TIMEOUT_S = 2.0
VERIFY_PROBABILITY = 0.97
VERIFY_DELAY_S = (0.1, 0.5)
LAB_TOKEN = "lab-ok"
SYNTHETIC_TXN_START = 900000000


def port() -> int:
    return BASE_PORT + int(os.environ.get("RF_PORT_OFFSET", "0"))


def base_url() -> str:
    return f"http://{TARGET_HOST}:{port()}"


def lab_rate() -> float:
    return float(os.environ.get("RF_LAB_RATE", "1.0"))
