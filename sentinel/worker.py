"""python -m sentinel.worker: reads edge.log and decisions, writes one `threat` entry every second (prd/04 SN-01)."""
from __future__ import annotations

import logging
import math
import os
import sys
import time

from .detector import Sentinel, threat_fields

log = logging.getLogger("sentinel")
GRACE_S = 0.5     # entries for second w are accepted until w + 1 + GRACE_S
MAXLEN = 100000


def run(r, clock=time.time, max_windows=None) -> None:
    det = Sentinel()
    ids = {}
    for stream in ("edge.log", "decisions"):
        newest = r.xrevrange(stream, count=1)
        ids[stream] = newest[0][0] if newest else "0-0"  # "$" would skip entries between reads
    window = math.floor(clock())
    emitted = 0
    while max_windows is None or emitted < max_windows:
        close_at = window + 1 + GRACE_S
        wait_ms = max(1, int((close_at - clock()) * 1000))
        resp = r.xread(ids, count=5000, block=min(wait_ms, 200))
        for stream, entries in resp or []:
            for entry_id, f in entries:
                ids[stream] = entry_id
                if stream == "edge.log":
                    det.edge(f)
                else:
                    det.decision(f)
        if clock() >= close_at:
            entry = det.close(float(window))
            r.xadd("threat", threat_fields(entry), maxlen=MAXLEN, approximate=True)
            emitted += 1
            window += 1
            behind = math.floor(clock()) - 1 - window
            if behind > 2:  # after a VM stall the loop closes the missed windows back to back (one entry each)
                log.warning("sentinel %d windows behind; catching up", behind)


def main() -> int:
    import redis

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    r = redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    run(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
