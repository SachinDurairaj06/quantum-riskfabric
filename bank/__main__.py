"""python -m bank [serve|load]   (prd/03 Part 2, CONTRACTS section 11)

  serve  (default) the bank on 127.0.0.1:9000 plus RF_PORT_OFFSET
  load   opening balances and tiers into Valkey from $RF_DATA/bank/accounts.csv and tiers.csv
"""
from __future__ import annotations

import json
import logging
import os
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "serve"
    data_dir = os.environ.get("RF_DATA", "/srv/riskfabric/data")
    url = os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0")
    if cmd == "load":
        import redis

        from .load import load

        print(json.dumps(load(redis.Redis.from_url(url, decode_responses=True), data_dir)))
        return 0
    if cmd != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = 9000 + int(os.environ.get("RF_PORT_OFFSET", "0"))
    # ws/loop/http pinned to pure-Python: the services are plain HTTP, and this avoids uvloop/httptools/websockets
    # version clashes (e.g. the websockets ServerProtocol import error on the conda s390x build).
    uvicorn.run(create_app(data_dir=data_dir), host="127.0.0.1", port=port, log_level="warning",
                access_log=False, ws="none", loop="asyncio", http="h11")
    return 0


if __name__ == "__main__":
    sys.exit(main())
