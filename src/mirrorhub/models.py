"""SQLModel tables.

All datetimes are stored as naive UTC (timestamps are UTC everywhere in MirrorHub).

Recent SQLModel releases reject naive values on plain `datetime` fields with
"Datetime values must have timezone information". Every timestamp field therefore sets
`sa_type=UTCDateTime`: SQLModel still builds the column normally (primary keys, indexes,
defaults all keep working) but binds through our own type, which skips that check.

`UTCDateTime` writes naive UTC and always returns naive UTC on read.

Enum-like fields are stored as strings (values in mirrorhub.enums); validation lives in
the service layer.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Index, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from mirrorhub.enums import (
    CleanupMode,
    DiffMode,
    ExportFormat,
    ExportMode,
    ExportStatus,
    JobStatus,
    JobType,
    Method,
    RepoKind,
    Tool,
    Trigger,
)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite has no timezone support: store naive UTC, always return naive UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _json() -> Any:
    return Column(JSON, nullable=False)


def default_retry_policy() -> dict[str, Any]:
    return {
        "max_attempts": 3,
        "backoff": "exponential",  # fixed | linear | exponential
        "base_seconds": 60,
        "max_seconds": 3600,
        "retryable_exit_codes": [1, 2, 10, 11, 12, 20, 23, 24, 30, 35, 255],
    }


class Admin(SQLModel, table=True):
    __tablename__ = "admin"
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, max_length=64)
    password_hash: str
    failed_attempts: int = 0
    locked_until: datetime | None = Field(default=None, sa_type=UTCDateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class AuthSession(SQLModel, table=True):
    __tablename__ = "session"
    id: str = Field(primary_key=True, max_length=64)  # SHA-256 of the cookie token
    admin_id: int = Field(foreign_key="admin.id", index=True)
    csrf_secret: str
    ip: str = ""
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    expires_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)


class Repository(SQLModel, table=True):
    __tablename__ = "repository"
    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True, max_length=64)
    name: str = Field(max_length=128)
    kind: str = RepoKind.APT.value
    tool: str = Tool.DEBMIRROR.value
    enabled: bool = True

    upstream_host: str = ""
    upstream_root: str = ""
    method: str = Method.HTTPS.value
    suites: list[str] = Field(default_factory=list, sa_column=_json())
    sections: list[str] = Field(default_factory=list, sa_column=_json())
    architectures: list[str] = Field(default_factory=list, sa_column=_json())
    include_source: bool = False
    rsync_extra: list[str] = Field(default_factory=list, sa_column=_json())
    extra_files: list[dict[str, str]] = Field(default_factory=list, sa_column=_json())

    bwlimit_kbps: int = 0
    diff_mode: str = DiffMode.USE.value
    cleanup_mode: str = CleanupMode.POST.value
    verify_signatures: bool = True
    keyring_name: str = "trustedkeys.gpg"
    include_patterns: list[str] = Field(default_factory=list, sa_column=_json())
    exclude_patterns: list[str] = Field(default_factory=list, sa_column=_json())
    include_regex: str = ""
    exclude_regex: str = ""
    extra_args: list[str] = Field(default_factory=list, sa_column=_json())

    retry_policy: dict[str, Any] = Field(default_factory=default_retry_policy, sa_column=_json())

    export_mode: str = ExportMode.INCR.value
    export_format: str = ExportFormat.TAR.value
    export_split_mb: int = 0
    chain_export: bool = False

    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=UTCDateTime,
        sa_column_kwargs={"onupdate": utcnow},
    )


class Preset(SQLModel, table=True):
    """Editable seed data, cloned into repositories. Not hard-coded behavior."""

    __tablename__ = "preset"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, max_length=128)
    kind: str = RepoKind.APT.value
    config: dict[str, Any] = Field(default_factory=dict, sa_column=_json())
    is_seed: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_claim", "status", "tool", "next_run_at"),
        Index("ix_job_repo_status", "repo_id", "status"),
    )
    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id")
    type: str = JobType.SYNC.value
    tool: str = Tool.DEBMIRROR.value
    trigger: str = Trigger.MANUAL.value
    dry_run: bool = False
    params: dict[str, Any] = Field(default_factory=dict, sa_column=_json())

    status: str = Field(default=JobStatus.QUEUED.value, index=True)
    attempt: int = 1
    max_attempts: int = 1
    next_run_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    claimed_by: str | None = None
    heartbeat_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    started_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    exit_code: int | None = None
    progress_pct: float | None = None
    progress_text: str = ""
    log_path: str = ""
    parent_job_id: int | None = Field(default=None, foreign_key="job.id")
    cancel_requested: bool = False
    error_summary: str = ""
    requested_by: str = "system"


class RepoLock(SQLModel, table=True):
    """Primary key on repo_id guarantees at most one active run per repository."""

    __tablename__ = "repo_lock"
    repo_id: int = Field(primary_key=True, foreign_key="repository.id")
    job_id: int = Field(foreign_key="job.id", unique=True)
    worker_id: str
    acquired_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    heartbeat_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Schedule(SQLModel, table=True):
    __tablename__ = "schedule"
    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    job_type: str = JobType.SYNC.value
    cron_expr: str = "0 3 * * *"
    preset_label: str = ""
    enabled: bool = True
    chain_export: bool = False
    last_fire_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    next_fire_at: datetime | None = Field(default=None, sa_type=UTCDateTime)


class Export(SQLModel, table=True):
    __tablename__ = "export"
    __table_args__ = (UniqueConstraint("repo_id", "export_id"),)
    id: int | None = Field(default=None, primary_key=True)
    export_id: str = Field(index=True)  # e.g. 20261103T201500Z-incr
    repo_id: int = Field(foreign_key="repository.id", index=True)
    mode: str = ExportMode.INCR.value
    base_export_id: str | None = None
    path: str = ""
    status: str = ExportStatus.BUILDING.value
    file_count: int = 0
    total_bytes: int = 0
    deleted_count: int = 0
    signed: bool = False
    job_id: int | None = Field(default=None, foreign_key="job.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class ManifestSnapshot(SQLModel, table=True):
    """Compressed JSONL file list on the state volume. The newest snapshot linked to a
    published export of a repo is the diff base for the next incremental export."""

    __tablename__ = "manifest_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    repo_id: int = Field(foreign_key="repository.id", index=True)
    job_id: int | None = Field(default=None, foreign_key="job.id")
    export_id: str | None = None
    path: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class GpgKey(SQLModel, table=True):
    __tablename__ = "gpg_key"
    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(unique=True, index=True)
    uid: str = ""
    source: str = "upload"  # upload | package
    keyring_name: str = "trustedkeys.gpg"
    imported_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class NotificationChannel(SQLModel, table=True):
    __tablename__ = "notification_channel"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, max_length=64)
    type: str
    config: dict[str, Any] = Field(default_factory=dict, sa_column=_json())  # secrets encrypted
    enabled: bool = True


class NotificationRule(SQLModel, table=True):
    __tablename__ = "notification_rule"
    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="notification_channel.id", index=True)
    event: str
    repo_id: int | None = Field(default=None, foreign_key="repository.id")


class AuditLog(SQLModel, table=True):
    """Append-only (DB triggers in migration 0001 reject UPDATE and DELETE)."""

    __tablename__ = "audit_log"
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    actor: str = Field(index=True)
    ip: str = ""
    action: str = Field(index=True)
    target_type: str = ""
    target_id: str = ""
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=_json())


class Setting(SQLModel, table=True):
    __tablename__ = "setting"
    key: str = Field(primary_key=True, max_length=64)
    value: Any = Field(default=None, sa_column=Column(JSON))


class StorageCache(SQLModel, table=True):
    __tablename__ = "storage_cache"
    repo_id: int = Field(primary_key=True, foreign_key="repository.id")
    bytes: int = 0
    per_arch_bytes: dict[str, int] = Field(default_factory=dict, sa_column=_json())
    computed_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
