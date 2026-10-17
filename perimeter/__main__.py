"""python -m perimeter [serve|limit]

  serve  (default) the perimeter stand-in on 127.0.0.1:8000 plus RF_PORT_OFFSET, in front of the edge on 8080
  limit  the global ceiling from this host's measured capacity; --set writes it to Valkey
"""
from __future__ import annotations

import logging
import os
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "serve"
    if cmd == "limit":
        from .limit import main as limit_main

        return limit_main(argv[1:])
    if cmd != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    import redis.asyncio as aioredis
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    r = aioredis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    port = 8000 + int(os.environ.get("RF_PORT_OFFSET", "0"))
    uvicorn.run(create_app(redis=r), host="127.0.0.1", port=port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
