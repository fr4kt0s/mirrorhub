"""Let schedules inherit chain_export from their repository.

schedule.chain_export becomes nullable: NULL uses repository.chain_export,
true/false override it. Existing rows keep their effective behaviour: a schedule
whose value equals its repository's becomes NULL (inherit), a schedule that
differs keeps its value as an explicit override.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _nullable() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("schedule")
    return next(column for column in columns if column["name"] == "chain_export")["nullable"]


def upgrade() -> None:
    if not _nullable():
        with op.batch_alter_table("schedule") as batch:
            batch.alter_column("chain_export", existing_type=sa.Boolean(), nullable=True)

    op.execute(
        "UPDATE schedule SET chain_export = NULL "
        "WHERE chain_export = ("
        "SELECT repository.chain_export FROM repository "
        "WHERE repository.id = schedule.repo_id)"
    )


def downgrade() -> None:
    # Resolve inheritance to the repository's current value, then restore NOT NULL.
    op.execute(
        "UPDATE schedule SET chain_export = COALESCE(("
        "SELECT repository.chain_export FROM repository "
        "WHERE repository.id = schedule.repo_id), 0) "
        "WHERE chain_export IS NULL"
    )
    if _nullable():
        with op.batch_alter_table("schedule") as batch:
            batch.alter_column("chain_export", existing_type=sa.Boolean(), nullable=False)
