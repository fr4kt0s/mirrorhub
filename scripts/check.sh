#!/bin/sh
# Run local release checks. Stop at the first failed gate.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
else
    PYTHON=${PYTHON:-python3.12}
fi

"$PYTHON" -c '
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit("MirrorHub release checks require Python 3.12")
'

"$PYTHON" -m compileall -q src migrations tests
"$PYTHON" -m ruff check .
"$PYTHON" -m ruff format --check .
"$PYTHON" -m mypy
"$PYTHON" -m pytest

docker compose config --quiet

printf '%s\n' \
    "Local checks passed." \
    "Docker build, smoke tests and the release checklist remain separate gates."