"""Container healthchecks (stdlib only). Usage: python -m mirrorhub.healthcheck web|worker"""

import sys
import time
import urllib.request
from pathlib import Path

WORKER_ALIVE_FILE = Path("/tmp/worker.alive")  # noqa: S108 (tmpfs inside the container)
WORKER_MAX_AGE_S = 60


def check_web() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=4) as resp:
            return bool(resp.status == 200)
    except Exception:
        return False


def check_worker() -> bool:
    try:
        age = time.time() - WORKER_ALIVE_FILE.stat().st_mtime
    except OSError:
        return False
    return age < WORKER_MAX_AGE_S


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in {"web", "worker"}:
        print("usage: python -m mirrorhub.healthcheck web|worker", file=sys.stderr)
        return 2
    ok = check_web() if argv[0] == "web" else check_worker()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
