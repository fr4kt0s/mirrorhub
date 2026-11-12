"""Execution context supplied to sync adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings


@dataclass(frozen=True, slots=True)
class SyncExecutionContext:
    """Runtime inputs that are not part of the persistent sync configuration."""

    settings: Settings
    job: Job
    repository: Repository
    target: Path
    work_dir: Path
    log_dir: Path
