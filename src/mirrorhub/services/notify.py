"""Notifications: email, webhooks and event files for the data diode.

Channels are SMTP email, generic webhooks (ntfy, Gotify, Slack, plain JSON) and
JSON event files in the drop folder for transfer through the data diode.

Events are queued in the notification_delivery outbox, one row per (event, channel).
A background sender delivers due rows and retries failures with backoff, so a slow or
unreachable target never blocks the scheduler and no event is lost while it is down.
Each job is marked individually (job.notified_at) once its events are queued.

Secrets inside channel configs are encrypted with a key derived from the app secret, so
the SQLite file never holds a plaintext SMTP password or webhook token.
Delivery failures are logged and reported but never raised: a broken channel must not
break a job.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.enums import ChannelType, JobStatus, NotifyEvent
from mirrorhub.models import (
    Export,
    Job,
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
    Repository,
    utcnow,
)
from mirrorhub.services import appsettings, audit, logs
from mirrorhub.settings import Settings, get_secret_key, get_settings

log = logging.getLogger("mirrorhub.notify")

SECRET_FIELDS = ("password", "token", "url")
DISK_STATE_KEY = "disk_alert_level"

WEBHOOK_FORMATS = ("generic", "ntfy", "gotify", "slack")
LOG_MODES = ("none", "tail", "full")
EVENTS_DIRNAME = "_events"
STALE_TMP_S = 3600

# Delay before attempt n+1 after n failed attempts; then the delivery is given up.
BACKOFF_S = (60, 300, 900, 1800, 3600, 7200, 14400)
MAX_ATTEMPTS = len(BACKOFF_S) + 1
DELIVERY_RETENTION_DAYS = 30
SENDER_INTERVAL_S = 5


class NotifyError(RuntimeError):
    """A channel configuration is invalid or a delivery failed."""

    pass


@dataclass(frozen=True)
class Delivery:
    """Result of one delivery attempt to a channel."""

    channel: str
    ok: bool
    detail: str = ""


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_secret_key().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the channel config with its secret fields encrypted (``enc:`` prefix)."""
    out = dict(config)
    for key in SECRET_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value and not value.startswith("enc:"):
            out[key] = "enc:" + _fernet().encrypt(value.encode()).decode()
    return out


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the channel config with its secret fields decrypted."""
    out = dict(config)
    for key in SECRET_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value.startswith("enc:"):
            try:
                out[key] = _fernet().decrypt(value[4:].encode()).decode()
            except InvalidToken as exc:
                raise NotifyError(f"cannot decrypt '{key}'") from exc
    return out


def _int(config: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    raw = config.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise NotifyError(f"{key} must be a whole number") from exc
    if not low <= value <= high:
        raise NotifyError(f"{key} must be between {low} and {high}")
    return value


def validate_config(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Check and normalise a channel config; raises NotifyError with a form message."""
    if channel_type == ChannelType.WEBHOOK.value:
        url = str(config.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise NotifyError("webhook URL must start with https:// (or http://) and name a host")
        style = str(config.get("format") or "generic")
        if style not in WEBHOOK_FORMATS:
            raise NotifyError(f"webhook format must be one of {', '.join(WEBHOOK_FORMATS)}")
        return {"url": url, "format": style, "token": str(config.get("token") or "")}

    if channel_type == ChannelType.SMTP.value:
        host = str(config.get("host") or "").strip()
        if not host:
            raise NotifyError("SMTP host is required")
        recipient = str(config.get("to") or "").strip()
        sender = str(config.get("from") or config.get("username") or "").strip()
        if not recipient or not sender:
            raise NotifyError("SMTP from and to are required")
        if config.get("username") and not config.get("tls"):
            raise NotifyError(
                "SMTP login needs TLS: enable TLS, otherwise the password is sent in clear text"
            )
        return {
            "host": host,
            "port": _int(config, "port", 587, 1, 65535),
            "username": str(config.get("username") or ""),
            "password": str(config.get("password") or ""),
            "from": sender,
            "to": recipient,
            "tls": bool(config.get("tls")),
        }

    if channel_type == ChannelType.FILE.value:
        mode = str(config.get("log_mode") or "tail")
        if mode not in LOG_MODES:
            raise NotifyError(f"log detail must be one of {', '.join(LOG_MODES)}")
        return {
            "log_mode": mode,
            "log_lines": _int(config, "log_lines", 50, 1, 1000),
            "retention_days": _int(config, "retention_days", 14, 1, 365),
        }

    raise NotifyError("channel type must be smtp, webhook or file")


def create_channel(
    db: Session,
    *,
    name: str,
    channel_type: str,
    config: dict[str, Any],
    actor: str,
    ip: str = "",
) -> NotificationChannel:
    """Validate and store a new channel (secrets encrypted); NotifyError on a duplicate name."""
    clean = validate_config(channel_type, config)
    if db.exec(select(NotificationChannel).where(NotificationChannel.name == name[:64])).first():
        raise NotifyError(f"a channel named {name[:64]!r} already exists")
    channel = NotificationChannel(
        name=name[:64], type=channel_type, config=encrypt_config(clean), enabled=True
    )
    db.add(channel)
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="notify.channel_create",
        target_type="notification_channel",
        target_id=name,
        detail={"type": channel_type, "fields": sorted(clean)},
        commit=False,
    )
    db.commit()
    db.refresh(channel)
    return channel


def describe_channel(channel: NotificationChannel) -> str:
    """Human-readable target without secrets."""
    try:
        config = decrypt_config(channel.config or {})
    except NotifyError:
        return "(cannot decrypt config)"
    if channel.type == ChannelType.WEBHOOK.value:
        parsed = urlparse(str(config.get("url") or ""))
        return f"{config.get('format', 'generic')} → {parsed.scheme}://{parsed.netloc}"
    if channel.type == ChannelType.SMTP.value:
        return f"{config.get('to', '')} via {config.get('host', '')}:{config.get('port', '')}"
    if channel.type == ChannelType.FILE.value:
        mode = config.get("log_mode", "tail")
        detail = f"log {mode}" + (f" {config.get('log_lines', 50)} lines" if mode == "tail" else "")
        return f"/export/{EVENTS_DIRNAME}/ ({detail}, keep {config.get('retention_days', 14)} days)"
    return channel.type


def list_channels(db: Session) -> list[NotificationChannel]:
    """Return all channels, by name."""
    return list(db.exec(select(NotificationChannel).order_by(col(NotificationChannel.name))).all())


def delete_channel(db: Session, channel: NotificationChannel, *, actor: str, ip: str = "") -> None:
    """Delete a channel together with its rules and queued deliveries."""
    for rule in db.exec(
        select(NotificationRule).where(NotificationRule.channel_id == channel.id)
    ).all():
        db.delete(rule)
    for delivery in db.exec(
        select(NotificationDelivery).where(NotificationDelivery.channel_id == channel.id)
    ).all():
        db.delete(delivery)
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="notify.channel_delete",
        target_type="notification_channel",
        target_id=channel.name,
        commit=False,
    )
    db.delete(channel)
    db.commit()


def create_rule(
    db: Session,
    *,
    channel_id: int,
    event: str,
    repo_id: int | None,
    actor: str,
    ip: str = "",
) -> NotificationRule:
    """Store a rule that sends an event (optionally of one repository) to a channel."""
    if event not in NotifyEvent:
        raise NotifyError("unknown notification event")
    if db.get(NotificationChannel, channel_id) is None:
        raise NotifyError("unknown channel")
    rule = NotificationRule(channel_id=channel_id, event=event, repo_id=repo_id)
    db.add(rule)
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="notify.rule_create",
        target_type="notification_rule",
        target_id=f"{channel_id}:{event}",
        detail={"repo_id": repo_id},
        commit=False,
    )
    db.commit()
    db.refresh(rule)
    return rule


def list_rules(db: Session) -> list[NotificationRule]:
    """Return all rules, oldest first."""
    return list(db.exec(select(NotificationRule).order_by(col(NotificationRule.id))).all())


def delete_rule(db: Session, rule: NotificationRule, *, actor: str, ip: str = "") -> None:
    """Delete a rule."""
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="notify.rule_delete",
        target_type="notification_rule",
        target_id=str(rule.id),
        commit=False,
    )
    db.delete(rule)
    db.commit()


def _matching(db: Session, event: str, repo_id: int | None) -> list[NotificationChannel]:
    rules = db.exec(select(NotificationRule).where(col(NotificationRule.event) == event)).all()
    channels: list[NotificationChannel] = []
    for rule in rules:
        if rule.repo_id is not None and rule.repo_id != repo_id:
            continue
        channel = db.get(NotificationChannel, rule.channel_id)
        if channel is not None and channel.enabled:
            channels.append(channel)
    return channels


def _http_post(url: str, payload: bytes, headers: dict[str, str], timeout: int = 10) -> int:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return int(response.status)


def _send_webhook(
    config: dict[str, Any], event: str, title: str, message: str, meta: dict[str, Any] | None = None
) -> None:
    url = str(config.get("url") or "")
    if not url:
        raise NotifyError("webhook url missing")
    style = str(config.get("format") or "generic")
    token = str(config.get("token") or "")
    meta = meta or {}

    if style == "ntfy":
        body = f"{title}\n{message}".encode()
        headers = {
            "Title": title,
            "Priority": str(config.get("priority", 3)),
            "Content-Type": "text/plain; charset=utf-8",
            "Tags": event,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif style == "gotify":
        body = json.dumps(
            {"title": title, "message": message, "priority": int(config.get("priority", 5))}
        ).encode()
        headers = {"Content-Type": "application/json"}
        # Header instead of ?token=, which would end up in proxy and server logs.
        if token:
            headers["X-Gotify-Key"] = token
        url = f"{url.rstrip('/')}/message"
    elif style == "slack":
        body = json.dumps({"text": f"*{title}*\n{message}"}).encode()
        headers = {"Content-Type": "application/json"}
    else:
        body = json.dumps({"event": event, "title": title, "message": message, **meta}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

    try:
        status = _http_post(url, body, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotifyError(f"webhook delivery failed: {exc}") from exc
    if status >= 400:
        raise NotifyError(f"webhook returned HTTP {status}")


def _send_smtp(config: dict[str, Any], subject: str, body: str) -> None:
    host = str(config.get("host") or "")
    if not host:
        raise NotifyError("smtp host missing")
    try:
        port = int(config.get("port") or 587)
    except ValueError as exc:
        raise NotifyError("smtp port is not a number") from exc
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    recipient = str(config.get("to") or "")
    sender = str(config.get("from") or username)
    if not recipient or not sender:
        raise NotifyError("smtp from/to missing")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    use_tls = bool(config.get("tls", True))
    if username and not use_tls:
        # Channels saved before the form check existed: never send a password in clear.
        raise NotifyError("smtp login without TLS refused; enable TLS for this channel")
    try:
        if use_tls and port == 465:
            with smtplib.SMTP_SSL(
                host, port, context=ssl.create_default_context(), timeout=15
            ) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"smtp delivery failed: {exc}") from exc


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def events_dir(settings: Settings | None = None) -> Path:
    """Return the drop-folder directory for event files (``<export>/_events``)."""
    return (settings or get_settings()).export_dir / EVENTS_DIRNAME


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(directory: Path, name: str, data: bytes) -> None:
    """Write a file atomically in directory.

    Writes a hidden temporary file and renames it, so a pickup tool never sees a
    partial file.
    """
    temporary = directory / f".{name}.tmp"
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / name)


def _atomic_copy(source: Path, directory: Path, name: str) -> None:
    temporary = directory / f".{name}.tmp"
    with source.open("rb") as reader, temporary.open("wb") as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(temporary, directory / name)


_NAME_PART = re.compile(r"[^a-z0-9._-]+")


def _write_event_file(
    config: dict[str, Any],
    event: str,
    title: str,
    message: str,
    meta: dict[str, Any],
    ref: str,
) -> str:
    directory = events_dir()
    directory.mkdir(parents=True, exist_ok=True)
    repo = _NAME_PART.sub("-", str(meta.get("repo") or "system").lower()).strip("-") or "system"
    base = f"{_stamp(utcnow())}-{event}-{repo}-{_NAME_PART.sub('-', ref)}"
    document: dict[str, Any] = {"event": event, "title": title, "message": message, **meta}

    job_id = meta.get("job_id")
    mode = str(config.get("log_mode") or "none")
    if mode != "none" and isinstance(job_id, int):
        log_file = logs.job_log_path(job_id)
        if not log_file.is_file():
            document["log_unavailable"] = True
        elif mode == "tail":
            # Only the job's latest run: retries append to the same log file.
            document["log_tail"] = logs.tail_current_run(
                log_file, int(config.get("log_lines") or 50)
            )
        else:
            _atomic_copy(log_file, directory, f"{base}.log")
            document["log_file"] = f"{base}.log"

    data = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
    _atomic_write(directory, f"{base}.json", data)
    _fsync_dir(directory)
    return f"{base}.json"


def send_channel(
    channel: NotificationChannel,
    subject: str,
    body: str,
    event: str = "test",
    meta: dict[str, Any] | None = None,
    *,
    ref: str = "test",
) -> Delivery:
    """Send one message through a channel now; a NotifyError becomes a failed Delivery."""
    try:
        config = decrypt_config(channel.config)
        if channel.type == ChannelType.SMTP.value:
            _send_smtp(config, subject, body)
        elif channel.type == ChannelType.FILE.value:
            try:
                name = _write_event_file(config, event, subject, body, meta or {}, ref)
            except OSError as exc:
                raise NotifyError(f"cannot write event file: {exc}") from exc
            return Delivery(channel.name, True, name)
        else:
            _send_webhook(config, event, subject, body, meta)
    except NotifyError as exc:
        return Delivery(channel.name, False, str(exc))
    return Delivery(channel.name, True)


def enqueue(
    db: Session,
    event: str,
    *,
    title: str,
    message: str,
    repo_id: int | None = None,
    job_id: int | None = None,
    meta: dict[str, Any] | None = None,
    commit: bool = True,
) -> list[NotificationDelivery]:
    """Queue one delivery per matching channel. The sender thread delivers them."""
    rows: list[NotificationDelivery] = []
    for channel in _matching(db, event, repo_id):
        row = NotificationDelivery(
            channel_id=channel.id or 0,
            event=event,
            title=title[:500],
            message=message[:4000],
            payload=dict(meta or {}),
            repo_id=repo_id,
            job_id=job_id,
        )
        db.add(row)
        rows.append(row)
    if commit:
        db.commit()
        for row in rows:
            db.refresh(row)
    else:
        db.flush()
    return rows


def deliver(delivery_id: int, now: datetime | None = None) -> Delivery | None:
    """Attempt one queued delivery. No database transaction is held while sending."""
    now = now or utcnow()
    engine = get_engine()
    with Session(engine) as db:
        row = db.get(NotificationDelivery, delivery_id)
        if row is None or row.status != "pending":
            return None
        channel = db.get(NotificationChannel, row.channel_id)
        if channel is None or not channel.enabled:
            row.status = "failed"
            row.last_error = "channel deleted or disabled"
            db.add(row)
            db.commit()
            return Delivery(str(row.channel_id), False, row.last_error)
        target = NotificationChannel(**channel.model_dump())
        event, title, message = row.event, row.title, row.message
        meta = dict(row.payload or {})

    result = send_channel(target, title, message, event, meta, ref=f"d{delivery_id}")

    with Session(engine) as db:
        row = db.get(NotificationDelivery, delivery_id)
        if row is None:
            return result
        row.attempts += 1
        if result.ok:
            row.status = "sent"
            row.sent_at = now
            row.last_error = ""
        else:
            row.last_error = result.detail[:500]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                audit.record(
                    db,
                    actor="notifier",
                    action="notify.delivery_failed",
                    target_type="notification_channel",
                    target_id=target.name,
                    detail={"event": event, "attempts": row.attempts, "error": row.last_error},
                    commit=False,
                )
            else:
                row.next_attempt_at = now + timedelta(seconds=BACKOFF_S[row.attempts - 1])
            log.warning(
                "notification failed",
                extra={
                    "channel": target.name,
                    "event": event,
                    "attempt": row.attempts,
                    "detail": result.detail,
                },
            )
        db.add(row)
        db.commit()
    return result


def deliver_due(now: datetime | None = None, limit: int = 50) -> list[Delivery]:
    """Deliver the outbox rows that are due (pending, next attempt passed)."""
    now = now or utcnow()
    with Session(get_engine()) as db:
        ids = list(
            db.exec(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.status == "pending",
                    col(NotificationDelivery.next_attempt_at) <= now,
                )
                .order_by(col(NotificationDelivery.id))
                .limit(limit)
            ).all()
        )
    results: list[Delivery] = []
    for delivery_id in ids:
        if delivery_id is None:
            continue
        result = deliver(delivery_id, now)
        if result is not None:
            results.append(result)
    return results


def dispatch(
    db: Session,
    event: str,
    *,
    title: str,
    message: str,
    repo_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> list[Delivery]:
    """Queue and deliver immediately (used by tests and one-off calls)."""
    rows = enqueue(db, event, title=title, message=message, repo_id=repo_id, meta=meta)
    results: list[Delivery] = []
    for row in rows:
        result = deliver(row.id or 0)
        if result is not None:
            results.append(result)
    return results


def test_channel(db: Session, channel: NotificationChannel) -> Delivery:
    """Send a test message through a channel (Test button)."""
    return send_channel(
        channel,
        "MirrorHub test notification",
        "This is a test message from MirrorHub. If you can read this, the channel works.",
        "test",
        {"channel": channel.name, "time": _iso(utcnow())},
        ref="test",
    )


def _job_payload(job: Job, repo: Repository | None) -> dict[str, Any]:
    return {
        "time": _iso(utcnow()),
        "repo": repo.slug if repo else None,
        "repo_id": job.repo_id,
        "job_id": job.id,
        "job_type": job.type,
        "trigger": job.trigger,
        "dry_run": job.dry_run,
        "status": job.status,
        "exit_code": job.exit_code,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_summary": job.error_summary,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def _queue_job(db: Session, job: Job) -> list[NotificationDelivery]:
    repo = db.get(Repository, job.repo_id)
    label = repo.slug if repo else f"repo {job.repo_id}"
    payload = _job_payload(job, repo)
    rows: list[NotificationDelivery] = []

    def queue(event: str, title: str, message: str, extra: dict[str, Any] | None = None) -> None:
        rows.extend(
            enqueue(
                db,
                event,
                title=title,
                message=message,
                repo_id=job.repo_id,
                job_id=job.id,
                meta={**payload, **(extra or {})},
                commit=False,
            )
        )

    if job.status == JobStatus.SUCCEEDED.value:
        queue(
            NotifyEvent.JOB_SUCCEEDED.value,
            f"Mirror job {job.id} succeeded",
            f"{label}: {job.type}"
            + (" (dry run)" if job.dry_run else "")
            + f" finished successfully after {job.attempt} attempt(s).",
        )
        if job.type == "export":
            export = db.exec(select(Export).where(Export.job_id == job.id)).first()
            if export is not None and export.status == "published":
                queue(
                    NotifyEvent.EXPORT_PUBLISHED.value,
                    f"Export {export.export_id} published",
                    f"{label}: {export.mode} export {export.export_id} with "
                    f"{export.file_count} files ({export.total_bytes / 1e9:.2f} GB), "
                    f"{export.deleted_count} deletions.",
                    {
                        "export_id": export.export_id,
                        "export_mode": export.mode,
                        "base_export_id": export.base_export_id,
                        "file_count": export.file_count,
                        "deleted_count": export.deleted_count,
                        "archive_bytes": export.total_bytes,
                        "signed": export.signed,
                    },
                )
    else:
        # exit_code is None when the job failed before a tool ran (configuration error).
        reason = f"exit code {job.exit_code}. " if job.exit_code is not None else ""
        queue(
            NotifyEvent.JOB_FAILED.value,
            f"Mirror job {job.id} failed",
            f"{label}: {reason}{job.error_summary}",
        )
        if job.attempt >= job.max_attempts:
            queue(
                NotifyEvent.RETRY_EXHAUSTED.value,
                f"Mirror job {job.id}: retries exhausted",
                f"{label}: gave up after {job.attempt} attempt(s).",
            )
    return rows


def sweep_finished(db: Session) -> list[NotificationDelivery]:
    """Queue notifications for every finished job not yet handled, in any order.

    Each job is marked with notified_at in the same transaction as its queued
    deliveries, so a job is handled exactly once even if jobs finish out of order.
    """
    jobs = db.exec(
        select(Job)
        .where(
            col(Job.notified_at).is_(None),
            col(Job.status).in_([JobStatus.SUCCEEDED.value, JobStatus.FAILED.value]),
        )
        .order_by(col(Job.id))
    ).all()
    queued: list[NotificationDelivery] = []
    for job in jobs:
        queued.extend(_queue_job(db, job))
        job.notified_at = utcnow()
        db.add(job)
        db.commit()
    return queued


def check_disk(db: Session, usage: Any) -> list[NotificationDelivery]:
    """Queue one low-disk notification per threshold crossing."""
    values = appsettings.snapshot(db)
    warn = float(values.get("disk_warn_percent", 85))
    critical = float(values.get("disk_critical_percent", 95))
    level = appsettings.get(db, DISK_STATE_KEY, "ok")
    percent = float(usage.percent_used)

    if percent >= critical:
        new_level = "critical"
    elif percent >= warn:
        new_level = "warn"
    else:
        new_level = "ok"
    if new_level == level:
        return []
    appsettings.set_value(db, DISK_STATE_KEY, new_level)
    if new_level == "ok":
        return []
    return enqueue(
        db,
        NotifyEvent.LOW_DISK.value,
        title=f"Disk space {new_level} on {usage.path}",
        message=f"{percent:.1f}% used ({usage.used_bytes / 1e9:.1f} GB of "
        f"{usage.total_bytes / 1e9:.1f} GB), {new_level} threshold reached.",
        meta={
            "time": _iso(utcnow()),
            "percent": percent,
            "path": usage.path,
            "level": new_level,
            "used_bytes": usage.used_bytes,
            "total_bytes": usage.total_bytes,
        },
    )


def recent_deliveries(db: Session, limit: int = 25) -> list[NotificationDelivery]:
    """Return the newest outbox rows, for the notifications page."""
    return list(
        db.exec(
            select(NotificationDelivery).order_by(col(NotificationDelivery.id).desc()).limit(limit)
        ).all()
    )


def purge_deliveries(db: Session, now: datetime | None = None) -> int:
    """Delete sent and failed outbox rows older than DELIVERY_RETENTION_DAYS."""
    cutoff = (now or utcnow()) - timedelta(days=DELIVERY_RETENTION_DAYS)
    rows = db.exec(
        select(NotificationDelivery).where(
            col(NotificationDelivery.status).in_(["sent", "failed"]),
            col(NotificationDelivery.created_at) < cutoff,
        )
    ).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


def purge_event_files(
    db: Session, settings: Settings | None = None, now: float | None = None
) -> int:
    """Delete event files the pickup tool left behind, and stale temporary files.

    Event files are kept for the longest retention of any file channel (default 14
    days).
    """
    directory = events_dir(settings)
    if not directory.is_dir() or directory.is_symlink():
        return 0
    days = [
        int(decrypt_config(channel.config).get("retention_days", 14))
        for channel in list_channels(db)
        if channel.type == ChannelType.FILE.value
    ]
    keep_s = max(days or [14]) * 86400
    current = now if now is not None else time.time()
    removed = 0
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        limit = STALE_TMP_S if path.name.endswith(".tmp") else keep_s
        try:
            if path.stat().st_mtime < current - limit:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


async def run_sender_forever() -> None:
    """Deliver queued notifications every few seconds, off the scheduler loop."""
    log.info("notification sender started", extra={"interval_s": SENDER_INTERVAL_S})
    while True:
        try:
            results = await asyncio.to_thread(deliver_due)
            if results:
                log.info(
                    "notifications delivered",
                    extra={
                        "sent": sum(1 for result in results if result.ok),
                        "failed": sum(1 for result in results if not result.ok),
                    },
                )
        except asyncio.CancelledError:
            log.info("notification sender stopped")
            raise
        except Exception:
            log.exception("notification sender pass failed")
        await asyncio.sleep(SENDER_INTERVAL_S)
