"""String enums shared by models, services and templates. Stored as plain strings in the DB."""

from enum import StrEnum


class RepoKind(StrEnum):
    """What a repository mirrors: an APT or Arch repository, or generic files."""

    APT = "apt"
    ARCH = "arch"
    GENERIC = "generic"


class Tool(StrEnum):
    """The sync tool of a repository; ``export`` marks jobs of the export worker."""

    DEBMIRROR = "debmirror"
    RSYNC = "rsync"
    WGET = "wget"
    FTPSYNC = "ftpsync"
    EXPORT = "export"


class Method(StrEnum):
    """Transport to the upstream mirror."""

    HTTP = "http"
    HTTPS = "https"
    RSYNC = "rsync"


class DiffMode(StrEnum):
    """debmirror's handling of pdiff index files (``--diff``)."""

    USE = "use"
    MIRROR = "mirror"
    NONE = "none"


class CleanupMode(StrEnum):
    """When debmirror removes files that are no longer in the upstream index."""

    POST = "post"  # debmirror default (--postcleanup), not passed explicitly
    CLEANUP = "cleanup"  # advanced
    NONE = "none"  # advanced (--nocleanup)


class JobType(StrEnum):
    """What a job does."""

    SYNC = "sync"
    EXPORT = "export"
    # Re-read every file and rebuild the hash cache; runs in the export worker.
    REHASH = "rehash"


# Job types a schedule (and a configuration file) may name.
SCHEDULE_JOB_TYPES = (JobType.SYNC.value, JobType.EXPORT.value, JobType.REHASH.value)


class JobStatus(StrEnum):
    """Life cycle of a job."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# A job in one of these states no longer runs and can be deleted.
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
)


class Trigger(StrEnum):
    """What started a job."""

    MANUAL = "manual"
    SCHEDULE = "schedule"
    CHAIN = "chain"
    RETRY = "retry"


class ExportMode(StrEnum):
    """Full export of the whole tree or incremental export since a base."""

    INCR = "incr"
    FULL = "full"


class ExportFormat(StrEnum):
    """Archive format of an export."""

    TAR = "tar"
    ZST = "tar.zst"


class ExportStatus(StrEnum):
    """Life cycle of an export record."""

    BUILDING = "building"
    PUBLISHED = "published"
    FAILED = "failed"


class ChannelType(StrEnum):
    """How a notification channel delivers."""

    SMTP = "smtp"
    WEBHOOK = "webhook"
    FILE = "file"


class NotifyEvent(StrEnum):
    """Events a notification rule can send."""

    JOB_FAILED = "job_failed"
    JOB_SUCCEEDED = "job_succeeded"
    RETRY_EXHAUSTED = "retry_exhausted"
    LOW_DISK = "low_disk"
    EXPORT_PUBLISHED = "export_published"
