"""Domain-level sync planning and adapter contracts."""

from mirrorhub.sync.adapter import SyncAdapter, SyncAdapterRegistry
from mirrorhub.sync.adapters import FtpsyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError, SyncExecutionError
from mirrorhub.sync.plan import FtpsyncPlan, RsyncPlan, SyncPlan
from mirrorhub.sync.result import SyncResult, SyncStatus

__all__ = [
    "FtpsyncAdapter",
    "FtpsyncPlan",
    "RsyncPlan",
    "SyncAdapter",
    "SyncAdapterRegistry",
    "SyncConfigurationError",
    "SyncExecutionContext",
    "SyncExecutionError",
    "SyncPlan",
    "SyncResult",
    "SyncStatus",
]
