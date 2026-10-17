"""python -m console: serve the console on 127.0.0.1, port 8500 plus RF_PORT_OFFSET."""
import os

import uvicorn

from .app import create_app


def main() -> None:
    port = 8500 + int(os.environ.get("RF_PORT_OFFSET", "0"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
