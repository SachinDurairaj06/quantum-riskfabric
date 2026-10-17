"""python -m quantum.worker: run the shadow worker with RF_QML_MODE (shadow by default; off exits at once)."""
import logging
import os
import sys

from .worker import QDIR, RerankRefused, Worker


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mode = os.environ.get("RF_QML_MODE", "shadow")
    if mode == "off":
        print("RF_QML_MODE=off: quantum worker not started")
        return 0
    import redis

    r = redis.Redis.from_url(os.environ.get("RF_VALKEY_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    try:
        worker = Worker(r, os.path.join(QDIR, "models"), mode, os.path.join(QDIR, "GATE.md"))
    except RerankRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"quantum worker: {exc}", file=sys.stderr)
        return 3
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
