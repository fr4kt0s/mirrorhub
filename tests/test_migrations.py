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
        assert version == "0005"

    command.upgrade(config, "head")


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("prepend_sys_path", str(root / "src"))
    return config


def test_0005_makes_schedule_chain_export_inheritable(configured: Settings) -> None:
    from sqlalchemy import MetaData, inspect
    from sqlmodel import Session

    from mirrorhub.models import Repository, Schedule

    with Session(get_engine()) as db:
        db.add(Repository(id=1, slug="repo1", name="repo1", chain_export=True))
        db.add(Repository(id=2, slug="repo2", name="repo2", chain_export=False))
        db.commit()

    # Recreate the pre-0005 schedule table, where chain_export was NOT NULL.
    old = MetaData()
    Repository.__table__.to_metadata(old)  # type: ignore[attr-defined]  # FK target only
    table = Schedule.__table__.to_metadata(old)  # type: ignore[attr-defined]
    table.c.chain_export.nullable = False
    with get_engine().begin() as connection:
        connection.execute(text("DROP TABLE schedule"))
        old.create_all(connection, tables=[table])
        # (schedule id, repo id, schedule value) -> expected value after 0005
        for schedule_id, repo_id, chain in ((1, 1, 1), (2, 1, 0), (3, 2, 0), (4, 2, 1)):
            connection.execute(
                text(
                    "INSERT INTO schedule (id, repo_id, job_type, cron_expr, preset_label, "
                    "enabled, chain_export) VALUES (:id, :repo, 'sync', '0 3 * * *', '', 1, :chain)"
                ),
                {"id": schedule_id, "repo": repo_id, "chain": chain},
            )

    config = _alembic_config()
    command.stamp(config, "0004")
    command.upgrade(config, "head")

    with get_engine().connect() as connection:
        columns = {column["name"]: column for column in inspect(connection).get_columns("schedule")}
        assert columns["chain_export"]["nullable"] is True
        rows = dict(
            connection.execute(text("SELECT id, chain_export FROM schedule ORDER BY id")).all()
        )
    # Equal to the repository -> inherit (NULL); different -> explicit override kept.
    assert rows == {1: None, 2: 0, 3: None, 4: 1}
