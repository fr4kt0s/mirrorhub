"""Append-only audit log: write and query. The DB itself rejects UPDATE/DELETE (triggers)."""

from datetime import datetime
from typing import Any

from sqlmodel import Session, col, select

from mirrorhub.models import AuditLog, utcnow

_SECRET_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "private")
REDACTED = "***"


def redact(value: Any) -> Any:
    """Recursively mask values whose key looks like a secret."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if any(h in str(k).lower() for h in _SECRET_HINTS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    ip: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    """Add an audit row. Use commit=False to make it atomic with the change being audited."""
    row = AuditLog(
        ts=utcnow(),
        actor=actor[:64],
        ip=ip[:64],
        action=action[:64],
        target_type=target_type[:64],
        target_id=target_id[:128],
        detail=redact(detail or {}),
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def query(
    db: Session,
    *,
    actor: str = "",
    action: str = "",
    text: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditLog]:
    stmt = select(AuditLog)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(col(AuditLog.action).startswith(action))
    if text:
        stmt = stmt.where(col(AuditLog.target_id).contains(text))
    if since:
        stmt = stmt.where(AuditLog.ts >= since)
    if until:
        stmt = stmt.where(AuditLog.ts < until)
    stmt = stmt.order_by(col(AuditLog.id).desc()).offset(offset).limit(limit)
    return list(db.exec(stmt).all())
