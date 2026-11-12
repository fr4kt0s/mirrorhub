"""Uniform result contract for sync adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SyncErrorCode(StrEnum):
    CONFIG_INVALID = "config_invalid"
    AUTH_FAILED = "auth_failed"
    SOURCE_UNREACHABLE = "source_unreachable"
    NETWORK_TIMEOUT = "network_timeout"
    TRANSIENT_REMOTE_ERROR = "transient_remote_error"
    DISK_FULL = "disk_full"
    INTEGRITY_ERROR = "integrity_error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """A normalized result produced by a sync adapter."""

    status: SyncStatus
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    error_code: SyncErrorCode | None = None
    retryable: bool = False
    message: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in {
            SyncStatus.SUCCEEDED,
            SyncStatus.SUCCEEDED_WITH_WARNINGS,
        }
