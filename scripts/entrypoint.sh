#!/bin/sh
# Refuses to run as root (the init-volumes service opts out explicitly),
# applies DB migrations in the web role only, then execs the command.
set -eu

if [ "$(id -u)" = "0" ] && [ "${MIRRORHUB_ALLOW_ROOT:-0}" != "1" ]; then
    echo "mirrorhub: refusing to run as root (set PUID/PGID in .env)" >&2
    exit 64
fi

if [ "${MIRRORHUB_ROLE:-}" = "web" ]; then
    python -m mirrorhub.db migrate
fi

exec "$@"