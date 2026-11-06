"""Real migration execution and append-only audit triggers."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from mirrorhub.db import get_engine
from mirrorhub.settings import Settings


def test_migration_installs_append_only_audit_triggers(
    configured: Settings,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("prepend_sys_path", str(root / "src"))

    command.upgrade(config, "head")

    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log "
                "(ts, actor, ip, action, target_type, target_id, detail) "
                "VALUES "
                "('2026-11-03 00:00:00', 'test', '', 'test.append', '', '', '{}')"
            )
        )

    with pytest.raises(DatabaseError, match="append-only"), get_engine().begin() as connection:
        connection.execute(text("UPDATE audit_log SET actor = 'modified'"))

    with pytest.raises(DatabaseError, match="append-only"), get_engine().begin() as connection:
        connection.execute(text("DELETE FROM audit_log"))

    with get_engine().connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0001"

    command.upgrade(config, "head")
