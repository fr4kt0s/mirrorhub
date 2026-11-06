"""String enums shared by models, services and templates. Stored as plain strings in the DB."""

from enum import StrEnum


class RepoKind(StrEnum):
    APT = "apt"
    ARCH = "arch"
    GENERIC = "generic"


class Tool(StrEnum):
    DEBMIRROR = "debmirror"
    RSYNC = "rsync"
    WGET = "wget"
    EXPORT = "export"


class Method(StrEnum):
    HTTP = "http"
    HTTPS = "https"
    RSYNC = "rsync"


class DiffMode(StrEnum):
    USE = "use"
    MIRROR = "mirror"
    NONE = "none"


class CleanupMode(StrEnum):
    POST = "post"  # debmirror default (--postcleanup), not passed explicitly
    CLEANUP = "cleanup"  # advanced
    NONE = "none"  # advanced (--nocleanup)


class JobType(StrEnum):
    SYNC = "sync"
    EXPORT = "export"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Trigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    CHAIN = "chain"
    RETRY = "retry"


class ExportMode(StrEnum):
    INCR = "incr"
    FULL = "full"


class ExportFormat(StrEnum):
    TAR = "tar"
    ZST = "tar.zst"


class ExportStatus(StrEnum):
    BUILDING = "building"
    PUBLISHED = "published"
    FAILED = "failed"


class ChannelType(StrEnum):
    SMTP = "smtp"
    WEBHOOK = "webhook"


class NotifyEvent(StrEnum):
    JOB_FAILED = "job_failed"
    JOB_SUCCEEDED = "job_succeeded"
    RETRY_EXHAUSTED = "retry_exhausted"
    LOW_DISK = "low_disk"
