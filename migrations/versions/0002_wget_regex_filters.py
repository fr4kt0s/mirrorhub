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


def upgrade() -> None:
    op.add_column(
        "repository",
        sa.Column("include_regex", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "repository",
        sa.Column("exclude_regex", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("repository", "exclude_regex")
    op.drop_column("repository", "include_regex")
