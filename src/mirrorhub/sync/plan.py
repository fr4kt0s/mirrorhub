"""Validated, method-specific descriptions of a repository sync."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mirrorhub.enums import Tool


class SyncPlan(BaseModel):
    """Base model for all sync plans."""

    model_config = ConfigDict(frozen=True)

    job_id: int
    repository_id: int
    slug: str
    adapter: Tool
    source_protocol: str
    upstream_host: str
    upstream_root: str
    target: Path
    work_dir: Path
    log_dir: Path
    dry_run: bool = False
    attempt: int = Field(default=1, ge=1)
    credentials_ref: str | None = None


class FtpsyncPlan(SyncPlan):
    """Plan for an FTPsync-based Debian archive mirror."""

    adapter: Literal[Tool.FTPSYNC]
    source_protocol: Literal["rsync"]
    architectures: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()


class RsyncPlan(SyncPlan):
    """Plan for an rsync-based repository mirror."""

    adapter: Literal[Tool.RSYNC]
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()
