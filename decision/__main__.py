"""python -m decision: serve on 127.0.0.1 at RF_REPLICA plus RF_PORT_OFFSET."""
import uvicorn

from . import config
from .app import create_app_from_env


def main() -> None:
    cfg = config.load()
    port = int(cfg.replica) + cfg.port_offset
    uvicorn.run(create_app_from_env(), host="127.0.0.1", port=port, log_level="info", access_log=False,
                ws="none", loop="asyncio", http="h11")   # pure-Python protocols: avoids s390x extension version clashes


if __name__ == "__main__":
    main()
