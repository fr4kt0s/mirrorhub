"""Drop the unused account-wide lockout columns from admin.

Since 0004 the lockout lives in login_throttle per (username, IP). admin.failed_attempts
and admin.locked_until were only written, never read.

0001 builds the baseline from the current models, so fresh databases do not have the
columns; both directions check before they change anything.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "admin"
COLUMNS = ("failed_attempts", "locked_until")


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    present = [name for name in COLUMNS if name in _columns()]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in present:
            batch.drop_column(name)


def downgrade() -> None:
    existing = _columns()
    missing = [name for name in COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        if "failed_attempts" in missing:
            batch.add_column(
                sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0")
            )
        if "locked_until" in missing:
            batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
