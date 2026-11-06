"""Runtime settings stored in the setting table.

Environment variables supply defaults.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from mirrorhub.models import Setting
from mirrorhub.settings import get_settings


def _defaults() -> dict[str, Any]:
    return {
        "log_retention_days": get_settings().log_retention_days,
        "disk_warn_percent": 85,
        "disk_critical_percent": 95,
    }


def get(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    if default is not None:
        return default
    return _defaults().get(key)


def set_value(db: Session, key: str, value: Any, *, commit: bool = True) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
        db.add(row)
    if commit:
        db.commit()


def snapshot(db: Session) -> dict[str, Any]:
    values = _defaults()
    for row in db.exec(select(Setting)).all():
        values[row.key] = row.value
    return values
