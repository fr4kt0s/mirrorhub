#!/bin/sh
# Exercise the running deployment over trusted HTTPS.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
else
    PYTHON=${PYTHON:-python3.12}
fi

if [ -z "${MIRRORHUB_URL:-}" ]; then
    echo "Set MIRRORHUB_URL to the configured HTTPS origin." >&2
    echo "Example: MIRRORHUB_URL=https://mirrorhub.lan:8443" >&2
    exit 2
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

docker compose cp \
    proxy:/data/caddy/pki/authorities/local/root.crt \
    "$TEMP_DIR/root.crt"

"$PYTHON" scripts/smoke_test.py \
    --url "$MIRRORHUB_URL" \
    --ca "$TEMP_DIR/root.crt"