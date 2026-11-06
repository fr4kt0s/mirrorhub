"""Notification config encryption, webhook payloads, rule matching and the job sweep.

A channel only receives events that a rule routes to it, and a repo-scoped rule must
reference an existing repository (foreign key enforced by SQLite).
"""

import json

import pytest
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Job, NotificationChannel, Repository
from mirrorhub.services import notify


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_post(url: str, payload: bytes, headers: dict[str, str], timeout: int = 10) -> int:
        calls.append({"url": url, "payload": payload, "headers": headers})
        return 200

    monkeypatch.setattr(notify, "_http_post", fake_post)
    return calls


def _repo(db: Session, slug: str) -> int:
    repo = Repository(slug=slug, name=slug)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo.id or 0


def _channel(
    db: Session,
    name: str,
    *,
    url: str = "https://example/x",
    fmt: str = "generic",
    token: str = "",
) -> int:
    channel = notify.create_channel(
        db,
        name=name,
        channel_type="webhook",
        config={"url": url, "format": fmt, "token": token},
        actor="test",
    )
    assert isinstance(channel, NotificationChannel)
    return channel.id or 0


def test_config_secrets_are_encrypted_at_rest(configured) -> None:
    config = {"url": "https://ntfy.example/topic", "token": "s3cret", "format": "ntfy"}
    stored = notify.encrypt_config(config)
    assert stored["token"].startswith("enc:")
    assert stored["url"].startswith("enc:")
    assert stored["format"] == "ntfy"
    assert notify.decrypt_config(stored) == config


def test_channel_without_rules_sends_nothing(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        _channel(db, "hook")
        assert notify.dispatch(db, "job_failed", title="t", message="m") == []
    assert captured == []


def test_generic_webhook_payload(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        channel_id = _channel(db, "hook", url="https://hooks.example/x")
        notify.create_rule(
            db, channel_id=channel_id, event="job_failed", repo_id=None, actor="test"
        )
        deliveries = notify.dispatch(
            db, "job_failed", title="Job 1 failed", message="boom", meta={"job_id": 1}
        )
    assert len(deliveries) == 1
    assert deliveries[0].ok
    payload = json.loads(captured[0]["payload"])
    assert payload["title"] == "Job 1 failed"
    assert payload["event"] == "job_failed"
    assert payload["job_id"] == 1


def test_ntfy_payload_uses_headers(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        channel_id = _channel(
            db, "phone", url="https://ntfy.example/topic", fmt="ntfy", token="abc"
        )
        notify.create_rule(db, channel_id=channel_id, event="low_disk", repo_id=None, actor="test")
        notify.dispatch(db, "low_disk", title="Disk", message="90% used")
    headers = captured[0]["headers"]
    assert headers["Title"] == "Disk"
    assert headers["Authorization"] == "Bearer abc"
    assert captured[0]["payload"].startswith(b"Disk")


def test_gotify_url_gets_message_endpoint(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        channel_id = _channel(db, "gotify", url="https://gotify.example", fmt="gotify", token="tok")
        notify.create_rule(
            db, channel_id=channel_id, event="job_succeeded", repo_id=None, actor="test"
        )
        notify.dispatch(db, "job_succeeded", title="ok", message="done")
    assert captured[0]["url"] == "https://gotify.example/message?token=tok"
    payload = json.loads(captured[0]["payload"])
    assert payload["title"] == "ok"


def test_only_matching_rules_fire(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        repo_a = _repo(db, "debian")
        repo_b = _repo(db, "ubuntu")
        left = _channel(db, "left", url="https://example/left")
        right = _channel(db, "right", url="https://example/right")
        notify.create_rule(db, channel_id=left, event="job_failed", repo_id=repo_a, actor="test")
        notify.create_rule(db, channel_id=right, event="job_succeeded", repo_id=None, actor="test")

        notify.dispatch(db, "job_failed", title="t", message="m", repo_id=repo_a)
        assert [call["url"] for call in captured] == ["https://example/left"]

        captured.clear()
        notify.dispatch(db, "job_failed", title="t", message="m", repo_id=repo_b)
        assert captured == []

        notify.dispatch(db, "job_succeeded", title="t", message="m", repo_id=repo_b)
        assert [call["url"] for call in captured] == ["https://example/right"]


def test_failed_delivery_is_reported_not_raised(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_http_post", broken)
    with Session(get_engine()) as db:
        channel_id = _channel(db, "hook")
        notify.create_rule(
            db, channel_id=channel_id, event="job_failed", repo_id=None, actor="test"
        )
        deliveries = notify.dispatch(db, "job_failed", title="t", message="m")
    assert len(deliveries) == 1
    assert deliveries[0].ok is False
    assert "connection refused" in deliveries[0].detail


def test_sweep_finished_is_idempotent(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        channel_id = _channel(db, "hook")
        notify.create_rule(
            db, channel_id=channel_id, event="job_failed", repo_id=None, actor="test"
        )
        notify.create_rule(
            db, channel_id=channel_id, event="retry_exhausted", repo_id=None, actor="test"
        )
        db.add(
            Job(
                repo_id=repo_id,
                tool="debmirror",
                status=JobStatus.FAILED.value,
                attempt=3,
                max_attempts=3,
                exit_code=1,
                error_summary="exit 1",
            )
        )
        db.commit()

        first = notify.sweep_finished(db)
        second = notify.sweep_finished(db)

    # job_failed plus retry_exhausted, both routed to the same channel
    assert len(first) == 2
    assert all(delivery.ok for delivery in first)
    assert second == []
    assert len(captured) == 2


def test_successful_job_notifies_once(configured, captured: list[dict]) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        channel_id = _channel(db, "hook")
        notify.create_rule(
            db, channel_id=channel_id, event="job_succeeded", repo_id=None, actor="test"
        )
        db.add(
            Job(
                repo_id=repo_id,
                tool="debmirror",
                status=JobStatus.SUCCEEDED.value,
                attempt=1,
                max_attempts=1,
                exit_code=0,
            )
        )
        db.commit()
        deliveries = notify.sweep_finished(db)
        assert len(deliveries) == 1
        assert notify.sweep_finished(db) == []
    assert json.loads(captured[0]["payload"])["title"].startswith("Mirror job")
