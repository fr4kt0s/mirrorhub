"""Concrete sync adapters."""

from mirrorhub.sync.adapters.base import SyncAdapter
from mirrorhub.sync.adapters.debmirror import DebmirrorAdapter
from mirrorhub.sync.adapters.ftpsync import FtpsyncAdapter
from mirrorhub.sync.adapters.rsync import RsyncAdapter
from mirrorhub.sync.adapters.wget import WgetAdapter

__all__ = [
    "DebmirrorAdapter",
    "FtpsyncAdapter",
    "RsyncAdapter",
    "SyncAdapter",
    "WgetAdapter",
]
