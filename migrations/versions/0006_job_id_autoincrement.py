"""Never reuse job ids (SQLite AUTOINCREMENT on job.id).

Without AUTOINCREMENT SQLite hands out max(id) + 1, so after deleting the newest
jobs (or all of them) a new job gets an id that was used before. Job ids name the
log files (<log_dir>/jobs/<id>.log, opened for append), so the new job showed the
old job's output, and the notification high-water mark (notified_job_id) skipped
the reused ids.

The table is rebuilt with AUTOINCREMENT and the sequence is seeded with the highest
id ever issued: the larger of the current job ids, every numeric job id in the
append-only audit log, and the notification high-water mark.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HIGHEST_ISSUED = """
SELECT MAX(value) FROM (
    SELECT COALESCE(MAX(id), 0) AS value FROM job
    UNION ALL
    SELECT COALESCE(MAX(CAST(target_id AS INTEGER)), 0) FROM audit_log
    WHERE target_type = 'job' AND target_id <> '' AND target_id NOT GLOB '*[^0-9]*'
    UNION ALL
    SELECT COALESCE(MAX(CAST(value AS INTEGER)), 0) FROM setting
    WHERE key = 'notified_job_id'
)
"""


def _has_autoincrement() -> bool:
    sql = (
        op.get_bind()
        .execute(sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job'"))
        .scalar_one()
    )
    return "AUTOINCREMENT" in sql.upper()


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_autoincrement():
        # Rebuild (copy, drop, rename). migrations/env.py runs migrations with
        # foreign_keys=OFF and runs foreign_key_check before commit, as SQLite's
        # table-rebuild procedure requires (repo_lock, export, manifest_snapshot
        # and job.parent_job_id reference job.id).
        with op.batch_alter_table(
            "job",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": True},
        ):
            pass

    highest = int(bind.execute(sa.text(_HIGHEST_ISSUED)).scalar_one() or 0)
    bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'job'"))
    bind.execute(
        sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('job', :seq)"),
        {"seq": highest},
    )


def downgrade() -> None:
    if _has_autoincrement():
        with op.batch_alter_table(
            "job",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": False},
        ):
            pass
    op.execute("DELETE FROM sqlite_sequence WHERE name = 'job'")
