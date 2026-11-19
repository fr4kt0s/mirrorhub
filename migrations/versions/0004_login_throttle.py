"""Add per-(username, IP) login throttling.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "login_throttle"


def _exists() -> bool:
    return TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # 0001 builds the baseline from the current models, so fresh databases may
    # already have this table.
    if _exists():
        return
    op.create_table(
        TABLE,
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_throttle_username", TABLE, ["username"])
    op.create_index("ix_login_throttle_updated_at", TABLE, ["updated_at"])


def downgrade() -> None:
    if _exists():
        op.drop_index("ix_login_throttle_updated_at", table_name=TABLE)
        op.drop_index("ix_login_throttle_username", table_name=TABLE)
        op.drop_table(TABLE)
