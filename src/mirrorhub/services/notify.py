"""Notifications: SMTP email and generic webhooks (ntfy, Gotify, Slack, or plain JSON).

Secrets inside channel configs are encrypted with a key derived from the app secret, so
the SQLite file never holds a plaintext SMTP password or webhook token.
Delivery failures are logged and reported but never raised: a broken channel must not
break a job.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, col, select

from mirrorhub.enums import ChannelType, JobStatus, NotifyEvent
from mirrorhub.models import Job, NotificationChannel, NotificationRule
from mirrorhub.services import appsettings, audit
from mirrorhub.settings import get_secret_key

log = logging.getLogger("mirrorhub.notify")

SECRET_FIELDS = ("password", "token", "url")
HIGH_WATER_KEY = "notified_job_id"
DISK_STATE_KEY = "disk_alert_level"


class NotifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Delivery:
    channel: str
    ok: bool
    detail: str = ""


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_secret_key().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    for key in SECRET_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value and not value.startswith("enc:"):
            out[key] = "enc:" + _fernet().encrypt(value.encode()).decode()
    return out


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    for key in SECRET_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value.startswith("enc:"):
            try:
                out[key] = _fernet().decrypt(value[4:].encode()).decode()
            except InvalidToken as exc:
                raise NotifyError(f"cannot decrypt '{key}'") from exc
    return out


def create_channel(
    db: Session,
    *,
    name: str,
    channel_type: str,
    config: dict[str, Any],
    actor: str,
    ip: str = "",
) -> NotificationChannel:
    if channel_type not in (ChannelType.SMTP.value, ChannelType.WEBHOOK.value):
        raise NotifyError("channel type must be smtp or webhook")
    channel = NotificationChannel(
        name=name[:64], type=channel_type, config=encrypt_config(config), enabled=True
    )
    db.add(channel)
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="notify.channel_create",
        target_type="notification_channel",
        target_id=name,
        detail={"type": channel_type, "fields": sorted(config)},
        commit=False,
    )
    db.commit()
    db.refresh(channel)
    return channel


def list_channels(db: Session) -> list[NotificationChannel]:
    return list(db.exec(select(NotificationChannel).order_by(col(NotificationChannel.name))).all())


def delete_channel(db: Session, channel: NotificationChannel, *, actor: str, ip: str = "") -> None:
    for rule in db.exec(
        select(NotificationRule).where(NotificationRule.channel_id == channel.id)
    ).all():
        db.delete(rule)
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
    if event not in NotifyEvent:
        raise NotifyError("unknown notification event")
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
    return list(db.exec(select(NotificationRule).order_by(col(NotificationRule.id))).all())


def delete_rule(db: Session, rule: NotificationRule, *, actor: str, ip: str = "") -> None:
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
        url = f"{url.rstrip('/')}/message" + (f"?token={token}" if token else "")
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
    port = int(config.get("port") or 587)
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


def send_channel(
    channel: NotificationChannel,
    subject: str,
    body: str,
    event: str = "test",
    meta: dict[str, Any] | None = None,
) -> Delivery:
    try:
        config = decrypt_config(channel.config)
        if channel.type == ChannelType.SMTP.value:
            _send_smtp(config, subject, body)
        else:
            _send_webhook(config, event, subject, body, meta)
    except NotifyError as exc:
        return Delivery(channel.name, False, str(exc))
    return Delivery(channel.name, True)


def dispatch(
    db: Session,
    event: str,
    *,
    title: str,
    message: str,
    repo_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> list[Delivery]:
    deliveries: list[Delivery] = []
    for channel in _matching(db, event, repo_id):
        delivery = send_channel(channel, title, message, event, meta)
        if not delivery.ok:
            log.warning(
                "notification failed",
                extra={"channel": channel.name, "event": event, "detail": delivery.detail},
            )
        deliveries.append(delivery)
    return deliveries


def test_channel(db: Session, channel: NotificationChannel) -> Delivery:
    return send_channel(
        channel,
        "MirrorHub test notification",
        "This is a test message from MirrorHub. If you can read this, the channel works.",
        NotifyEvent.JOB_SUCCEEDED.value,
        {"channel": channel.name},
    )


def _high_water(db: Session) -> int:
    value = appsettings.get(db, HIGH_WATER_KEY, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def sweep_finished(db: Session) -> list[Delivery]:
    """Notify about jobs finished since the last sweep. Idempotent via a persisted mark."""
    start = _high_water(db)
    jobs = db.exec(
        select(Job)
        .where(
            col(Job.id) > start,
            col(Job.status).in_([JobStatus.SUCCEEDED.value, JobStatus.FAILED.value]),
        )
        .order_by(col(Job.id))
    ).all()
    deliveries: list[Delivery] = []
    highest = start
    for job in jobs:
        highest = max(highest, job.id or highest)
        repo_label = f"repo {job.repo_id}"
        if job.status == JobStatus.SUCCEEDED.value:
            deliveries.extend(
                dispatch(
                    db,
                    NotifyEvent.JOB_SUCCEEDED.value,
                    title=f"Mirror job {job.id} succeeded",
                    message=f"{repo_label}: {job.type}"
                    + (" (dry run)" if job.dry_run else "")
                    + f" finished successfully after {job.attempt} attempt(s).",
                    repo_id=job.repo_id,
                    meta={"job_id": job.id, "repo_id": job.repo_id},
                )
            )
        else:
            exhausted = job.attempt >= job.max_attempts
            deliveries.extend(
                dispatch(
                    db,
                    NotifyEvent.JOB_FAILED.value,
                    title=f"Mirror job {job.id} failed",
                    message=f"{repo_label}: exit code {job.exit_code}. {job.error_summary}",
                    repo_id=job.repo_id,
                    meta={"job_id": job.id, "repo_id": job.repo_id},
                )
            )
            if exhausted:
                deliveries.extend(
                    dispatch(
                        db,
                        NotifyEvent.RETRY_EXHAUSTED.value,
                        title=f"Mirror job {job.id}: retries exhausted",
                        message=f"{repo_label}: gave up after {job.attempt} attempt(s).",
                        repo_id=job.repo_id,
                        meta={"job_id": job.id, "repo_id": job.repo_id},
                    )
                )
    if highest > start:
        appsettings.set_value(db, HIGH_WATER_KEY, highest)
    return deliveries


def check_disk(db: Session, usage: Any) -> list[Delivery]:
    """Dispatch one low-disk notification per threshold crossing."""
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
    return dispatch(
        db,
        NotifyEvent.LOW_DISK.value,
        title=f"Disk space {new_level} on {usage.path}",
        message=f"{percent:.1f}% used ({usage.used_bytes / 1e9:.1f} GB of "
        f"{usage.total_bytes / 1e9:.1f} GB), {new_level} threshold reached.",
        meta={"percent": percent, "path": usage.path, "level": new_level},
    )
