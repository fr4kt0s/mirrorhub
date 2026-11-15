"""Add FTPsync ARCH_EXCLUDE.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("repository")}


def upgrade() -> None:
    if "arch_exclude" not in _existing_columns():
        op.add_column(
            "repository",
            sa.Column("arch_exclude", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if "arch_exclude" in _existing_columns():
        op.drop_column("repository", "arch_exclude")
