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
        assert version == "0011"

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
        rows: dict[int, int | None] = dict(
            connection.execute(text("SELECT id, chain_export FROM schedule ORDER BY id")).all()
        )
    # Equal to the repository -> inherit (NULL); different -> explicit override kept.
    assert rows == {1: None, 2: 0, 3: None, 4: 1}


def test_0007_marks_old_jobs_and_keeps_recent_unnotified_ones(configured: Settings) -> None:
    from datetime import timedelta

    from sqlmodel import Session

    from mirrorhub.models import Job, Repository, utcnow
    from mirrorhub.services import appsettings

    now = utcnow()
    with Session(get_engine()) as db:
        db.add(Repository(id=1, slug="repo", name="repo"))
        db.commit()
        rows = [
            # (id, status, finished_at)
            (1, "succeeded", now - timedelta(hours=1)),  # <= old mark -> marked
            (2, "failed", now - timedelta(hours=1)),  # <= old mark -> marked
            (3, "failed", now - timedelta(hours=1)),  # above mark, recent -> stays unmarked
            (4, "succeeded", now - timedelta(days=3)),  # above mark, old -> marked
            (5, "cancelled", now - timedelta(minutes=5)),  # cancelled -> marked
            (6, "running", None),  # not finished -> unmarked
        ]
        for job_id, status, finished in rows:
            db.add(Job(id=job_id, repo_id=1, status=status, finished_at=finished))
        db.commit()
        appsettings.set_value(db, "notified_job_id", 2)

    config = _alembic_config()
    command.stamp(config, "0006")
    command.upgrade(config, "head")

    with get_engine().connect() as connection:
        marked = {
            row[0]: row[1] is not None
            for row in connection.execute(text("SELECT id, notified_at FROM job ORDER BY id"))
        }
        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert marked == {1: True, 2: True, 3: False, 4: True, 5: True, 6: False}
    assert "notification_delivery" in tables


def test_0007_downgrade_keeps_job_autoincrement_and_sequence(configured: Settings) -> None:
    from sqlmodel import Session

    from mirrorhub.models import Job, Repository

    config = _alembic_config()
    command.upgrade(config, "head")
    with Session(get_engine()) as db:
        db.add(Repository(id=1, slug="repo", name="repo"))
        db.commit()
        for job_id in (1, 2, 3):
            db.add(Job(id=job_id, repo_id=1))
        db.commit()
        newest = db.get(Job, 3)
        assert newest is not None
        db.delete(newest)
        db.commit()

    command.downgrade(config, "0006")

    with get_engine().connect() as connection:
        table_sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job'")
        ).scalar_one()
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(job)")}
    assert "AUTOINCREMENT" in table_sql.upper()
    assert "notified_at" not in columns

    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (repo_id, type, tool, trigger, dry_run, params, "
                "status, attempt, max_attempts, next_run_at, created_at, "
                "progress_text, log_path, cancel_requested, error_summary, "
                "requested_by) VALUES (1, 'sync', 'debmirror', 'manual', 0, "
                "'{}', 'queued', 1, 1, '2026-11-20 00:00:00', "
                "'2026-11-20 00:00:00', '', '', 0, '', 'test')"
            )
        )
        new_id = connection.execute(text("SELECT MAX(id) FROM job")).scalar_one()
    assert new_id == 4  # id 3 is not reused after the downgrade


def test_0008_drops_unused_admin_columns_and_keeps_rows(configured: Settings) -> None:
    from datetime import timedelta

    from sqlmodel import Session

    from mirrorhub.models import Admin, AuthSession, utcnow

    config = _alembic_config()
    command.upgrade(config, "head")
    with Session(get_engine()) as db:
        db.add(Admin(id=1, username="admin", password_hash="x"))
        db.commit()
        now = utcnow()
        db.add(
            AuthSession(
                id="s" * 64,
                admin_id=1,
                csrf_secret="c" * 43,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        db.commit()

    def admin_columns() -> set[str]:
        with get_engine().connect() as connection:
            return {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(admin)")}

    assert not {"failed_attempts", "locked_until"} & admin_columns()

    command.downgrade(config, "0007")
    assert {"failed_attempts", "locked_until"} <= admin_columns()

    command.upgrade(config, "head")
    assert not {"failed_attempts", "locked_until"} & admin_columns()
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT username FROM admin")).scalar_one() == "admin"
        assert connection.execute(text("SELECT admin_id FROM session")).scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_0009_adds_wget_keep_arches_and_keeps_rows(configured: Settings) -> None:
    from sqlmodel import Session

    from mirrorhub.models import Repository

    config = _alembic_config()
    command.upgrade(config, "head")
    with Session(get_engine()) as db:
        db.add(
            Repository(slug="w", name="W", kind="generic", tool="wget", wget_keep_arches=["amd64"])
        )
        db.commit()

    def repo_columns() -> set[str]:
        with get_engine().connect() as connection:
            return {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(repository)")}

    assert "wget_keep_arches" in repo_columns()
    command.downgrade(config, "0008")
    assert "wget_keep_arches" not in repo_columns()

    command.upgrade(config, "head")
    with get_engine().connect() as connection:
        row = connection.exec_driver_sql("SELECT slug, wget_keep_arches FROM repository").one()
    assert tuple(row) == ("w", "[]")


def test_0010_adds_wget_custom_args_and_keeps_rows(configured: Settings) -> None:
    from sqlmodel import Session

    from mirrorhub.models import Repository

    config = _alembic_config()
    command.upgrade(config, "head")
    with Session(get_engine()) as db:
        db.add(
            Repository(
                slug="t", name="T", kind="generic", tool="wget", wget_custom_args=["--recursive"]
            )
        )
        db.commit()

    def repo_columns() -> set[str]:
        with get_engine().connect() as connection:
            return {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(repository)")}

    assert "wget_custom_args" in repo_columns()
    command.downgrade(config, "0009")
    assert "wget_custom_args" not in repo_columns()
    command.upgrade(config, "head")
    with get_engine().connect() as connection:
        row = connection.exec_driver_sql("SELECT slug, wget_custom_args FROM repository").one()
    assert tuple(row) == ("t", "[]")
