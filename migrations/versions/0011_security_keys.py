"""Add security keys (FIDO2/WebAuthn) and pending WebAuthn challenges.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Create both tables unless the baseline (0001, from the models) already did."""
    tables = _tables()
    if "security_key" not in tables:
        op.create_table(
            "security_key",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin.id"), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("credential_id", sa.String(length=1400), nullable=False),
            sa.Column("public_key", sa.String(), nullable=False),
            sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("aaguid", sa.String(), nullable=False, server_default=""),
            sa.Column("transports", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_security_key_admin_id", "security_key", ["admin_id"])
        op.create_index(
            "ix_security_key_credential_id", "security_key", ["credential_id"], unique=True
        )
    if "webauthn_challenge" not in tables:
        op.create_table(
            "webauthn_challenge",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("purpose", sa.String(length=16), nullable=False),
            sa.Column("challenge", sa.String(), nullable=False),
            sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin.id"), nullable=True),
            sa.Column("name", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_webauthn_challenge_expires_at", "webauthn_challenge", ["expires_at"]
        )


def downgrade() -> None:
    """Drop both tables (registered keys are lost)."""
    tables = _tables()
    if "webauthn_challenge" in tables:
        op.drop_index("ix_webauthn_challenge_expires_at", table_name="webauthn_challenge")
        op.drop_table("webauthn_challenge")
    if "security_key" in tables:
        op.drop_index("ix_security_key_credential_id", table_name="security_key")
        op.drop_index("ix_security_key_admin_id", table_name="security_key")
        op.drop_table("security_key")
