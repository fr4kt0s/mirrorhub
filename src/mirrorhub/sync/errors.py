"""Typed errors for sync planning and adapter execution."""

from __future__ import annotations

from mirrorhub.sync.result import SyncErrorCode


class SyncError(Exception):
    """Base class for sync-domain errors."""

    code: SyncErrorCode = SyncErrorCode.UNKNOWN


class SyncConfigurationError(SyncError):
    """A sync plan cannot be built from the supplied configuration."""

    code = SyncErrorCode.CONFIG_INVALID


class SyncExecutionError(SyncError):
    """A sync adapter could not execute or interpret a sync."""

    code = SyncErrorCode.UNKNOWN
