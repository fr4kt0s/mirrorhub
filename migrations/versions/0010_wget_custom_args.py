"""Add repository.wget_custom_args (hand-edited wget command options).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("repository")}


def upgrade() -> None:
    if "wget_custom_args" not in _existing_columns():
        op.add_column(
            "repository",
            sa.Column("wget_custom_args", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if "wget_custom_args" in _existing_columns():
        op.drop_column("repository", "wget_custom_args")
