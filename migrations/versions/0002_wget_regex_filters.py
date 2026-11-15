"""Add optional wget regex filters.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("repository")}


def upgrade() -> None:
    existing = _existing_columns()

    if "include_regex" not in existing:
        op.add_column(
            "repository",
            sa.Column("include_regex", sa.String(), nullable=False, server_default=""),
        )

    if "exclude_regex" not in existing:
        op.add_column(
            "repository",
            sa.Column("exclude_regex", sa.String(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    existing = _existing_columns()

    if "exclude_regex" in existing:
        op.drop_column("repository", "exclude_regex")

    if "include_regex" in existing:
        op.drop_column("repository", "include_regex")
