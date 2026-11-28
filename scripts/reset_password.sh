#!/usr/bin/env bash
# Reset the admin login on the host (needs shell access; never possible from the web UI).
#
#   scripts/reset_password.sh                         new password, all sessions ended
#   scripts/reset_password.sh --remove-security-keys  new password and remove all security keys
#   scripts/reset_password.sh --security-keys-only    remove all security keys, keep the password
#
# Every variant lifts all login lockouts. Removing the keys also sets the login mode back
# to "password or security key", so the password alone logs in again.
set -euo pipefail

mode=password
case "${1:-}" in
  "") ;;
  --remove-security-keys) mode=password-and-keys ;;
  --security-keys-only) mode=keys-only ;;
  -h|--help)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown option: $1 (see --help)" >&2
    exit 2
    ;;
esac

docker compose exec -e MH_RESET_MODE="$mode" web /opt/venv/bin/python -c '
import getpass
import os
import warnings

from sqlmodel import select

from mirrorhub.db import immediate_session
from mirrorhub.models import Admin, AuthSession, LoginThrottle
from mirrorhub.security.auth import hash_password
from mirrorhub.services import security_keys
from mirrorhub.services.audit import record

warnings.simplefilter("error", getpass.GetPassWarning)
mode = os.environ["MH_RESET_MODE"]

password_hash = None
if mode != "keys-only":
    password = getpass.getpass("New admin password (12-256 characters): ")
    confirmation = getpass.getpass("Confirm new password: ")

    if password != confirmation:
        raise SystemExit("Passwords do not match; nothing changed.")
    if not 12 <= len(password) <= 256:
        raise SystemExit("Password must contain 12-256 characters; nothing changed.")

    password_hash = hash_password(password)
    del password, confirmation

with immediate_session() as db:
    accounts = list(db.exec(select(Admin)).all())
    if len(accounts) != 1:
        raise SystemExit("Expected exactly one admin account; nothing changed.")

    admin = accounts[0]
    username = admin.username

    # Lift every login lockout, including those of the operator own address.
    throttles = db.exec(select(LoginThrottle)).all()
    for throttle in throttles:
        db.delete(throttle)

    sessions = []
    if password_hash is not None:
        admin.password_hash = password_hash
        db.add(admin)
        sessions = db.exec(
            select(AuthSession).where(AuthSession.admin_id == admin.id)
        ).all()
        for session in sessions:
            db.delete(session)

    keys_removed = 0
    if mode in ("password-and-keys", "keys-only"):
        keys_removed = security_keys.remove_all(db)
    keys_left = len(security_keys.all_keys(db))

    record(
        db,
        actor="operator-cli",
        action="admin.security_keys_reset" if mode == "keys-only" else "admin.password_reset",
        target_type="admin",
        target_id=username,
        detail={
            "sessions_revoked": len(sessions),
            "lockouts_lifted": len(throttles),
            "security_keys_removed": keys_removed,
        },
        commit=False,
    )

if password_hash is not None:
    print(f"Password reset completed for username: {username}")
if mode in ("password-and-keys", "keys-only"):
    print(f"Security keys removed: {keys_removed}; the password alone logs in again.")
elif keys_left:
    print(
        f"{keys_left} security key(s) stay registered. If they are lost, run "
        "scripts/reset_password.sh --security-keys-only"
    )
'
