"""Operator command that prunes the append-only audit log."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlmodel import Session, col, select

from mirrorhub import audit as auditcli
from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog, utcnow
from mirrorhub.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def _migrate() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(ROOT / "src"))
    command.upgrade(config, "head")


def _add(days_ago: int, action: str) -> None:
    with Session(get_engine()) as db:
        db.add(
            AuditLog(
                ts=utcnow() - timedelta(days=days_ago),
                actor="admin",
                action=action,
                detail={"n": days_ago},
            )
        )
        db.commit()


def _actions() -> list[str]:
    with Session(get_engine()) as db:
        return [row.action for row in db.exec(select(AuditLog).order_by(col(AuditLog.id))).all()]


def _trigger_sql() -> str | None:
    with get_engine().connect() as connection:
        row = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :n"),
            {"n": auditcli.DELETE_TRIGGER},
        ).first()
    return None if row is None else str(row[0])


@pytest.fixture
def migrated(configured: Settings) -> Settings:
    _migrate()
    _add(400, "old.one")
    _add(200, "old.two")
    _add(10, "recent")
    return configured


def test_purge_archives_deletes_and_restores_trigger(migrated: Settings, tmp_path: Path) -> None:
    before = _trigger_sql()
    archive = tmp_path / "archive.jsonl"

    result = auditcli.purge(180, archive)

    assert result.count == 2
    lines = [json.loads(line) for line in archive.read_text().splitlines()]
    assert [line["action"] for line in lines] == ["old.one", "old.two"]
    assert lines[0]["detail"] == {"n": 400}
    assert archive.stat().st_mode & 0o777 == 0o600
    assert _actions() == ["recent", "audit.purge"]
    assert _trigger_sql() == before == auditcli.CREATE_DELETE_TRIGGER

    with get_engine().begin() as connection, pytest.raises(DatabaseError, match="append-only"):
        connection.execute(text("DELETE FROM audit_log"))


def test_purge_entry_describes_itself(migrated: Settings, tmp_path: Path) -> None:
    auditcli.purge(180, tmp_path / "a.jsonl")
    with Session(get_engine()) as db:
        row = db.exec(select(AuditLog).where(AuditLog.action == "audit.purge")).one()
    assert row.actor == "cli"
    assert row.detail["deleted"] == 2
    assert row.detail["archive"] == str(tmp_path / "a.jsonl")


def test_minimum_is_90_days(migrated: Settings) -> None:
    with pytest.raises(auditcli.PurgeError, match="at least 90"):
        auditcli.purge(89)
    assert auditcli.main(["purge", "--older-than-days", "30"]) == 2
    assert len(_actions()) == 3


def test_dry_run_changes_nothing(migrated: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert auditcli.main(["purge", "--older-than-days", "90", "--dry-run"]) == 0
    assert "2 audit entries" in capsys.readouterr().out
    assert len(_actions()) == 3


def test_existing_archive_aborts_without_changes(migrated: Settings, tmp_path: Path) -> None:
    archive = tmp_path / "exists.jsonl"
    archive.write_text("keep\n")
    assert auditcli.main(["purge", "--older-than-days", "90", "--archive", str(archive)]) == 2
    assert archive.read_text() == "keep\n"
    assert len(_actions()) == 3
    assert _trigger_sql() == auditcli.CREATE_DELETE_TRIGGER


def test_failure_after_drop_rolls_back_and_keeps_trigger(
    migrated: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(auditcli.audit, "record", broken)
    with pytest.raises(RuntimeError, match="boom"):
        auditcli.purge(90, tmp_path / "a.jsonl")
    assert len(_actions()) == 3
    assert _trigger_sql() == auditcli.CREATE_DELETE_TRIGGER


def test_default_archive_goes_to_state_backups(migrated: Settings) -> None:
    result = auditcli.purge(180)
    assert result.archive is not None
    assert result.archive.parent == migrated.state_dir / "backups"
    assert result.archive.name.startswith("audit-archive-")


def test_refuses_without_trigger(configured: Settings) -> None:
    # create_all without migrations: no trigger, so the log is not protected
    with pytest.raises(auditcli.PurgeError, match="missing"):
        auditcli.purge(90)


def test_nothing_to_do(migrated: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert auditcli.main(["purge", "--older-than-days", "1000"]) == 0
    assert "no audit entries" in capsys.readouterr().out
