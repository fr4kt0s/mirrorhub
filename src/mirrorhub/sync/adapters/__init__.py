"""Concrete sync adapters."""

from mirrorhub.sync.adapters.ftpsync import FtpsyncAdapter
from mirrorhub.sync.adapters.rsync import RsyncAdapter

__all__ = ["FtpsyncAdapter", "RsyncAdapter"]
