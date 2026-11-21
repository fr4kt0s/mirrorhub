"""Validated, method-specific descriptions of a repository sync."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """Plan for an FTPsync-based Debian archive mirror.

    ``architectures`` maps to ARCH_INCLUDE and ``arch_exclude`` to ARCH_EXCLUDE.
    ftpsync accepts at most one of them; with neither it mirrors all architectures.
    """

    adapter: Literal[Tool.FTPSYNC]
    source_protocol: Literal["rsync"]
    architectures: tuple[str, ...] = ()
    arch_exclude: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_architecture_selection(self) -> FtpsyncPlan:
        if self.architectures and self.arch_exclude:
            raise ValueError("use either ARCH_INCLUDE or ARCH_EXCLUDE, not both")
        if "none" in self.architectures or "none" in self.arch_exclude:
            raise ValueError(
                "'none' is not an ftpsync architecture; leave both empty to mirror all"
            )
        return self


class RsyncPlan(SyncPlan):
    """Plan for an rsync-based repository mirror."""

    adapter: Literal[Tool.RSYNC]
    source_protocol: Literal["rsync"]
    repository_kind: str
    suites: tuple[str, ...] = ()
    architectures: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()


class DebmirrorPlan(SyncPlan):
    """Plan for a Debian or Ubuntu repository mirrored with debmirror."""

    adapter: Literal[Tool.DEBMIRROR]
    source_protocol: Literal["http", "https", "rsync"]
    architectures: tuple[str, ...] = Field(min_length=1)
    suites: tuple[str, ...] = Field(min_length=1)
    sections: tuple[str, ...] = Field(min_length=1)
    include_source: bool = False
    diff_mode: str
    verify_signatures: bool = False
    cleanup_mode: str
    rsync_extra: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()


class WgetPlan(SyncPlan):
    """Plan for a complete HTTP directory-tree mirror retrieved with wget."""

    adapter: Literal[Tool.WGET]
    source_protocol: Literal["http", "https"]
    include_regex: str = ""
    exclude_regex: str = ""
    extra_files: tuple[tuple[str, str], ...] = ()
    bwlimit_kbps: int = Field(default=0, ge=0)
    extra_args: tuple[str, ...] = ()
