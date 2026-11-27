"""Operator command to prune old audit log entries (``python -m mirrorhub.audit``).

The audit log is append-only: triggers from migration 0001 reject UPDATE and
DELETE, so neither the web interface nor a bug in the application can remove
entries. Only an operator with access to the Docker host can prune it, with this
command, which in one transaction

1. writes the entries to be removed to a JSON Lines archive,
2. drops the delete trigger, deletes the entries and creates the trigger again,
3. records the purge itself in the audit log (action ``audit.purge``).

If any step fails, the transaction rolls back and the trigger stays in place.
Entries younger than MIN_DAYS are never removed.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, text
from sqlmodel import col, func, select

from mirrorhub.db import immediate_session
from mirrorhub.models import AuditLog, utcnow
from mirrorhub.services import audit
from mirrorhub.settings import get_settings

MIN_DAYS = 90

# Must match migration 0001.
DELETE_TRIGGER = "audit_log_no_delete"
CREATE_DELETE_TRIGGER = (
    f"CREATE TRIGGER {DELETE_TRIGGER} BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
)


class PurgeError(Exception):
    """The purge was refused (period too short, archive file exists)."""


@dataclass
class PurgeResult:
    """Outcome of a purge: the cutoff time, the entries removed and their archive."""

    cutoff: str
    count: int
    archive: Path | None


def default_archive() -> Path:
    """Return the default archive file: ``<state>/backups/audit-archive-<UTC time>.jsonl``."""
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return get_settings().state_dir / "backups" / f"audit-archive-{stamp}.jsonl"


def purge(days: int, archive: Path | None = None, *, dry_run: bool = False) -> PurgeResult:
    """Remove audit entries older than ``days`` days (at least MIN_DAYS)."""
    if days < MIN_DAYS:
        raise PurgeError(f"--older-than-days must be at least {MIN_DAYS}")
    cutoff = utcnow() - timedelta(days=days)
    older = col(AuditLog.ts) < cutoff

    with immediate_session() as db:
        connection = db.connection()  # same transaction; core statements and DDL
        present = connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
            {"name": DELETE_TRIGGER},
        ).first()
        if present is None:
            raise PurgeError(
                f"trigger {DELETE_TRIGGER} is missing, so the audit log is not protected; "
                "investigate before pruning"
            )
        if dry_run:
            count = db.exec(select(func.count()).select_from(AuditLog).where(older)).one()
            return PurgeResult(cutoff.isoformat(sep=" "), count, None)

        rows = db.exec(select(AuditLog).where(older).order_by(col(AuditLog.id))).all()
        if not rows:
            return PurgeResult(cutoff.isoformat(sep=" "), 0, None)

        target = archive or default_archive()
        target.parent.mkdir(parents=True, exist_ok=True)
        # "x": never overwrite an earlier archive.
        with target.open("x", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        target.chmod(0o600)

        connection.execute(text(f"DROP TRIGGER {DELETE_TRIGGER}"))
        connection.execute(delete(AuditLog).where(older))
        connection.execute(text(CREATE_DELETE_TRIGGER))
        audit.record(
            db,
            actor="cli",
            action="audit.purge",
            target_type="audit_log",
            target_id="*",
            detail={
                "older_than": cutoff.isoformat(sep=" "),
                "deleted": len(rows),
                "first_id": rows[0].id,
                "last_id": rows[-1].id,
                "archive": str(target),
            },
            commit=False,
        )
    return PurgeResult(cutoff.isoformat(sep=" "), len(rows), target)


def main(arguments: list[str] | None = None) -> int:
    """Run the ``python -m mirrorhub.audit`` command line; return the exit code."""
    parser = argparse.ArgumentParser(prog="python -m mirrorhub.audit", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prune = commands.add_parser("purge", help="remove audit entries older than N days")
    prune.add_argument("--older-than-days", type=int, required=True, metavar="N")
    prune.add_argument(
        "--archive",
        type=Path,
        help="JSON Lines file for the removed entries (must not exist; "
        "default <state>/backups/audit-archive-<UTC time>.jsonl)",
    )
    prune.add_argument("--dry-run", action="store_true", help="only count, change nothing")
    options = parser.parse_args(arguments)

    try:
        result = purge(options.older_than_days, options.archive, dry_run=options.dry_run)
    except (PurgeError, FileExistsError) as exc:
        print(f"error: {exc}")
        return 2
    if options.dry_run:
        print(f"{result.count} audit entries older than {result.cutoff} would be deleted")
    elif result.count == 0:
        print(f"no audit entries older than {result.cutoff}")
    else:
        print(
            f"deleted {result.count} audit entries older than {result.cutoff}; "
            f"archive: {result.archive}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
