"""Notification outbox: per-job marking, retries, file channel and validation."""

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.importer import cli as importer_cli
from mirrorhub.importer.config import Config as ImporterConfig
from mirrorhub.models import (
    AuditLog,
    Export,
    Job,
    NotificationChannel,
    NotificationDelivery,
    Repository,
    utcnow,
)
from mirrorhub.services import export_jobs, logs, notify
from mirrorhub.settings import Settings


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, payload: bytes, headers: dict[str, str], timeout: int = 10) -> int:
        calls.append({"url": url, "payload": json.loads(payload)})
        return 200

    monkeypatch.setattr(notify, "_http_post", fake_post)
    return calls


def _repo(db: Session, slug: str = "debian") -> int:
    repo = Repository(slug=slug, name=slug)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo.id or 0


def _webhook(db: Session, events: tuple[str, ...]) -> int:
    channel = notify.create_channel(
        db,
        name="hook",
        channel_type="webhook",
        config={"url": "https://hooks.example/x", "format": "generic"},
        actor="test",
    )
    for event in events:
        notify.create_rule(db, channel_id=channel.id or 0, event=event, repo_id=None, actor="test")
    return channel.id or 0


def _file_channel(db: Session, events: tuple[str, ...], **config: Any) -> int:
    channel = notify.create_channel(
        db, name="drop", channel_type="file", config=config, actor="test"
    )
    for event in events:
        notify.create_rule(db, channel_id=channel.id or 0, event=event, repo_id=None, actor="test")
    return channel.id or 0


def _job(db: Session, repo_id: int, status: str, **fields: Any) -> int:
    job = Job(repo_id=repo_id, tool="debmirror", status=status, **fields)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job.id or 0


def test_jobs_finishing_out_of_order_are_all_notified(
    configured: Settings, captured: list[dict[str, Any]]
) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        _webhook(db, ("job_succeeded", "job_failed"))
        slow = _job(db, repo_id, JobStatus.RUNNING.value)
        fast = _job(db, repo_id, JobStatus.SUCCEEDED.value)

        assert [row.job_id for row in notify.sweep_finished(db)] == [fast]

        job = db.get(Job, slow)
        assert job is not None
        job.status = JobStatus.FAILED.value
        job.exit_code = 23
        db.add(job)
        db.commit()
        assert [row.job_id for row in notify.sweep_finished(db)] == [slow]

    notify.deliver_due()
    assert sorted(call["payload"]["job_id"] for call in captured) == [slow, fast]


def test_failed_delivery_is_retried_with_backoff_then_given_up(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def broken(*_args: Any, **_kwargs: Any) -> int:
        attempts.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_http_post", broken)
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        _webhook(db, ("job_failed",))
        _job(db, repo_id, JobStatus.FAILED.value)
        notify.sweep_finished(db)

    now = utcnow()
    assert [result.ok for result in notify.deliver_due(now)] == [False]
    assert notify.deliver_due(now + timedelta(seconds=30)) == []  # backoff not over

    for delay in notify.BACKOFF_S:
        now += timedelta(seconds=delay + 1)
        notify.deliver_due(now)

    with Session(get_engine()) as db:
        row = db.exec(select(NotificationDelivery)).one()
        assert row.status == "failed"
        assert row.attempts == notify.MAX_ATTEMPTS
        assert "connection refused" in row.last_error
        actions = [entry.action for entry in db.exec(select(AuditLog)).all()]
        assert "notify.delivery_failed" in actions
    assert len(attempts) == notify.MAX_ATTEMPTS


def test_delivery_recovers_after_a_failure(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = {"fail": True}

    def flaky(*_args: Any, **_kwargs: Any) -> int:
        if outcome["fail"]:
            raise OSError("down")
        return 200

    monkeypatch.setattr(notify, "_http_post", flaky)
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        _webhook(db, ("job_succeeded",))
        _job(db, repo_id, JobStatus.SUCCEEDED.value)
        notify.sweep_finished(db)

    now = utcnow()
    notify.deliver_due(now)
    outcome["fail"] = False
    assert [result.ok for result in notify.deliver_due(now + timedelta(seconds=61))] == [True]
    with Session(get_engine()) as db:
        row = db.exec(select(NotificationDelivery)).one()
        assert (row.status, row.attempts, row.last_error) == ("sent", 2, "")


def test_disabled_channel_marks_delivery_failed(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        channel_id = _webhook(db, ("job_succeeded",))
        _job(db, repo_id, JobStatus.SUCCEEDED.value)
        notify.sweep_finished(db)
        channel = db.get(NotificationChannel, channel_id)
        assert channel is not None
        channel.enabled = False
        db.add(channel)
        db.commit()

    assert [result.ok for result in notify.deliver_due()] == [False]
    with Session(get_engine()) as db:
        assert db.exec(select(NotificationDelivery)).one().status == "failed"


@pytest.mark.parametrize(
    ("channel_type", "config", "message"),
    [
        ("webhook", {"url": "ftp://example/x"}, "webhook URL must start with https://"),
        ("webhook", {"url": "https://x", "format": "teams"}, "webhook format must be one of"),
        ("smtp", {"host": "mail", "port": "abc", "from": "a@x", "to": "b@x"}, "whole number"),
        ("smtp", {"host": "mail", "port": "70000", "from": "a@x", "to": "b@x"}, "between 1"),
        ("smtp", {"host": "", "from": "a@x", "to": "b@x"}, "SMTP host is required"),
        (
            "smtp",
            {"host": "mail", "from": "a@x", "to": "b@x", "username": "u", "password": "p"},
            "SMTP login needs TLS",
        ),
        ("file", {"log_mode": "debug"}, "log detail must be one of"),
        ("file", {"log_lines": "0"}, "log_lines must be between 1 and 1000"),
        ("pager", {}, "channel type must be smtp, webhook or file"),
    ],
)
def test_invalid_channel_config_is_rejected(
    configured: Settings, channel_type: str, config: dict[str, Any], message: str
) -> None:
    with Session(get_engine()) as db, pytest.raises(notify.NotifyError, match=message):
        notify.create_channel(db, name="x", channel_type=channel_type, config=config, actor="test")


def test_invalid_smtp_port_shows_form_error(admin: TestClient, csrf_token: str) -> None:
    response = admin.post(
        "/notifications/channels",
        data={
            "name": "mail",
            "type": "smtp",
            "host": "mail",
            "port": "abc",
            "from": "a@x",
            "to": "b@x",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    assert "port must be a whole number" in response.text


def _events(settings: Settings) -> Path:
    return settings.export_dir / notify.EVENTS_DIRNAME


def test_file_channel_writes_json_with_log_tail(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "ubuntu")
        _file_channel(db, ("job_failed",), log_mode="tail", log_lines=2)
        job_id = _job(db, repo_id, JobStatus.FAILED.value, exit_code=23, error_summary="boom")
        logs.job_log_path(job_id).write_text("one\ntwo\nthree\n", encoding="utf-8")
        notify.sweep_finished(db)

    assert [result.ok for result in notify.deliver_due()] == [True]
    files = sorted(_events(configured).iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    assert "-job_failed-ubuntu-" in files[0].name
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["event"] == "job_failed"
    assert document["repo"] == "ubuntu"
    assert document["job_id"] == job_id
    assert document["exit_code"] == 23
    assert document["error_summary"] == "boom"
    assert document["log_tail"] == ["two", "three"]
    assert document["finished_at"] is None or document["finished_at"].endswith("Z")


def test_file_channel_full_log_is_a_separate_file(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        _file_channel(db, ("job_succeeded",), log_mode="full")
        job_id = _job(db, repo_id, JobStatus.SUCCEEDED.value)
        logs.job_log_path(job_id).write_text("full log\n", encoding="utf-8")
        notify.sweep_finished(db)
    notify.deliver_due()

    names = sorted(path.name for path in _events(configured).iterdir())
    assert len(names) == 2
    json_name = next(name for name in names if name.endswith(".json"))
    document = json.loads((_events(configured) / json_name).read_text(encoding="utf-8"))
    assert document["log_file"] in names
    assert (_events(configured) / document["log_file"]).read_text() == "full log\n"
    assert not any(name.startswith(".") for name in names)  # no temporary files left


def test_file_channel_without_log(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        _file_channel(db, ("job_succeeded",), log_mode="none")
        job_id = _job(db, repo_id, JobStatus.SUCCEEDED.value)
        logs.job_log_path(job_id).write_text("ignored\n", encoding="utf-8")
        notify.sweep_finished(db)
    notify.deliver_due()

    (path,) = list(_events(configured).iterdir())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert "log_tail" not in document and "log_file" not in document


def test_export_published_event(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "docker")
        _file_channel(db, ("export_published",), log_mode="none")
        job_id = _job(db, repo_id, JobStatus.SUCCEEDED.value)
        job = db.get(Job, job_id)
        assert job is not None
        job.type = "export"
        db.add(job)
        db.add(
            Export(
                export_id="20261120T200000Z-incr",
                repo_id=repo_id,
                mode="incr",
                base_export_id="20261119T200000Z-full",
                status="published",
                file_count=12,
                deleted_count=3,
                total_bytes=2_000_000,
                job_id=job_id,
            )
        )
        db.commit()
        notify.sweep_finished(db)
    notify.deliver_due()

    (path,) = list(_events(configured).iterdir())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["event"] == "export_published"
    assert document["export_id"] == "20261120T200000Z-incr"
    assert document["base_export_id"] == "20261119T200000Z-full"
    assert (document["file_count"], document["deleted_count"]) == (12, 3)


def test_test_button_writes_a_file(configured: Settings) -> None:
    with Session(get_engine()) as db:
        channel_id = _file_channel(db, ())
        channel = db.get(NotificationChannel, channel_id)
        assert channel is not None
        delivery = notify.test_channel(db, channel)
    assert delivery.ok
    assert (_events(configured) / delivery.detail).is_file()


def test_purge_event_files_keeps_recent_ones(configured: Settings) -> None:
    directory = _events(configured)
    directory.mkdir(parents=True)
    old = directory / "old.json"
    recent = directory / "recent.json"
    stale_tmp = directory / ".x.json.tmp"
    for path in (old, recent, stale_tmp):
        path.write_text("{}", encoding="utf-8")
    two_hours = time.time() - 7200
    os.utime(stale_tmp, (two_hours, two_hours))
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))

    with Session(get_engine()) as db:
        _file_channel(db, (), retention_days=14)
        assert notify.purge_event_files(db, configured) == 2
    assert sorted(path.name for path in directory.iterdir()) == ["recent.json"]


def test_purge_deliveries_removes_only_old_finished_rows(configured: Settings) -> None:
    with Session(get_engine()) as db:
        channel_id = _webhook(db, ())
        old = utcnow() - timedelta(days=notify.DELIVERY_RETENTION_DAYS + 1)
        for status in ("sent", "failed", "pending"):
            db.add(
                NotificationDelivery(
                    channel_id=channel_id, event="job_failed", status=status, created_at=old
                )
            )
        db.add(NotificationDelivery(channel_id=channel_id, event="job_failed", status="sent"))
        db.commit()
        assert notify.purge_deliveries(db) == 2
        remaining = sorted(row.status for row in db.exec(select(NotificationDelivery)).all())
    assert remaining == ["pending", "sent"]


def test_events_directory_is_ignored_by_drop_folder_cleanup_and_importer(
    configured: Settings,
) -> None:
    directory = _events(configured)
    directory.mkdir(parents=True)
    with Session(get_engine()) as db:
        assert export_jobs.clean_drop_folder(db, configured) == []
    assert directory.is_dir()

    config = ImporterConfig(
        export_dir=configured.export_dir,
        mirror_root=configured.data_dir,
        state_dir=configured.state_dir / "importer",
        staging_dir=configured.state_dir / "staging",
    )
    assert importer_cli.list_slugs(config) == []


def test_file_channel_tail_contains_only_the_latest_run(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "mint")
        _file_channel(db, ("job_failed",), log_mode="tail", log_lines=50)
        job_id = _job(db, repo_id, JobStatus.FAILED.value, attempt=2, max_attempts=2)
        started = utcnow()
        logs.job_log_path(job_id).write_text(
            logs.run_header(job_id, 1, "mint", "sync", "ftpsync", "w1", started)
            + "first attempt failed\n"
            + logs.run_header(job_id, 2, "mint", "sync", "ftpsync", "w1", started)
            + "second attempt failed\n",
            encoding="utf-8",
        )
        notify.sweep_finished(db)
    notify.deliver_due()

    (path,) = list(_events(configured).iterdir())
    tail = json.loads(path.read_text(encoding="utf-8"))["log_tail"]
    assert tail[0].startswith(f"# run job {job_id} attempt 2 ")
    assert tail[1:] == ["second attempt failed"]


def test_smtp_login_without_tls_is_refused_for_old_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(notify.smtplib, "SMTP", no_connection)
    config = {
        "host": "mail",
        "port": 25,
        "username": "u",
        "password": "p",
        "to": "b@x",
        "tls": False,
    }
    with pytest.raises(notify.NotifyError, match="without TLS refused"):
        notify._send_smtp(config, "subject", "body")
