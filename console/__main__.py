"""python -m console: serve the console, port 8500 plus RF_PORT_OFFSET.

Binds to 127.0.0.1 by default (local only). Set RF_HOST=0.0.0.0 to listen on every interface, so a browser on another
machine can reach it at http://<this-host-ip>:8500 (open the port in the firewall first; this exposes the read-only
console on the network).
"""
import os

import uvicorn

from .app import create_app


def main() -> None:
    port = 8500 + int(os.environ.get("RF_PORT_OFFSET", "0"))
    host = os.environ.get("RF_HOST", "127.0.0.1")
    print(f"console on http://{host}:{port}  (set RF_HOST=0.0.0.0 to reach it from another machine)")
    uvicorn.run(create_app(), host=host, port=port, log_level="info", access_log=False,
                ws="none", loop="asyncio", http="h11")   # pure-Python protocols: avoids s390x extension version clashes


if __name__ == "__main__":
    main()
