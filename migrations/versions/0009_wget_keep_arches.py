"""Add repository.wget_keep_arches (architecture filter for wget directory trees).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("repository")}


def upgrade() -> None:
    if "wget_keep_arches" not in _existing_columns():
        op.add_column(
            "repository",
            sa.Column("wget_keep_arches", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if "wget_keep_arches" in _existing_columns():
        op.drop_column("repository", "wget_keep_arches")
