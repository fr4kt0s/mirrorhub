"""Reliable notifications: per-job marker and delivery outbox.

The old sweep remembered only the highest notified job id, so a job that finished
after a job with a higher id was never notified. job.notified_at now marks each job
individually, and notification_delivery queues every (event, channel) pair for a
background sender with retries.

Existing finished jobs are marked as notified so the upgrade does not resend old
events. Jobs above the old high-water mark that finished within the last day are left
unmarked, so their pending notifications still go out.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "notification_delivery"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _high_water_mark() -> int:
    row = (
        op.get_bind()
        .execute(sa.text("SELECT value FROM setting WHERE key = 'notified_job_id'"))
        .first()
    )
    if row is None or row[0] is None:
        return 0
    try:
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return int(value)
    except (TypeError, ValueError):
        return 0


def upgrade() -> None:
    inspector = _inspector()
    job_columns = {column["name"] for column in inspector.get_columns("job")}
    if "notified_at" not in job_columns:
        op.add_column("job", sa.Column("notified_at", sa.DateTime(), nullable=True))

    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "channel_id",
                sa.Integer(),
                sa.ForeignKey("notification_channel.id"),
                nullable=False,
            ),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False, server_default=""),
            sa.Column("message", sa.String(), nullable=False, server_default=""),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("repo_id", sa.Integer(), nullable=True),
            sa.Column("job_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_error", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_notification_delivery_channel_id", TABLE, ["channel_id"])
        op.create_index("ix_notification_delivery_due", TABLE, ["status", "next_attempt_at"])

    # Stored as naive UTC "YYYY-MM-DD HH:MM:SS[.ffffff]"; compare as text.
    cutoff = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    op.get_bind().execute(
        sa.text(
            "UPDATE job SET notified_at = COALESCE(finished_at, created_at) "
            "WHERE notified_at IS NULL "
            "AND status IN ('succeeded', 'failed', 'cancelled') "
            "AND (id <= :mark OR status = 'cancelled' "
            "     OR COALESCE(finished_at, created_at) < :cutoff)"
        ),
        {"mark": _high_water_mark(), "cutoff": cutoff},
    )


def downgrade() -> None:
    inspector = _inspector()
    if TABLE in inspector.get_table_names():
        op.drop_index("ix_notification_delivery_due", table_name=TABLE)
        op.drop_index("ix_notification_delivery_channel_id", table_name=TABLE)
        op.drop_table(TABLE)
    if "notified_at" in {column["name"] for column in inspector.get_columns("job")}:
        with op.batch_alter_table("job") as batch:
            batch.drop_column("notified_at")
