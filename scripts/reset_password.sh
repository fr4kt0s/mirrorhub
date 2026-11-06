#!/usr/bin/env bash
set -euo pipefail

docker compose exec web /opt/venv/bin/python -c '
import getpass
import warnings

from sqlmodel import select

from mirrorhub.db import immediate_session
from mirrorhub.models import Admin, AuthSession
from mirrorhub.security.auth import hash_password
from mirrorhub.services.audit import record

warnings.simplefilter("error", getpass.GetPassWarning)

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
    admin.password_hash = password_hash
    admin.failed_attempts = 0
    admin.locked_until = None
    db.add(admin)

    sessions = db.exec(
        select(AuthSession).where(AuthSession.admin_id == admin.id)
    ).all()
    for session in sessions:
        db.delete(session)

    record(
        db,
        actor="operator-cli",
        action="admin.password_reset",
        target_type="admin",
        target_id=username,
        detail={"sessions_revoked": len(sessions)},
        commit=False,
    )

print(f"Password reset completed for username: {username}")
'