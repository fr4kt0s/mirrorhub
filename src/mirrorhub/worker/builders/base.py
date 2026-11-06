"""Shared builder types. Lives outside _init_.py so submodules never import the package root.

This module must not import anything from mirrorhub.worker.builders: doing so re-enters a
partially initialized package and produces a circular-import error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class BuildPlan:
    steps: list[Step]
    cwd: Path
    target: Path
    warnings: list[str] = field(default_factory=list)


def resolve_paths(repo: Repository, job: Job, settings: Settings) -> Path:
    """Mirror root for this repo. Dry runs go to the state volume instead of the data mount."""
    if job.dry_run:
        base = settings.tmp_dir / "dryrun" / f"{repo.slug}-{job.id or 0}"
    else:
        base = settings.data_dir / repo.slug
        root = settings.data_dir.resolve()
        resolved = base.resolve()
        if resolved != root and root not in resolved.parents:
            raise BuildError(f"target path escapes the data mount: {resolved}")
    base.mkdir(parents=True, exist_ok=True)
    return base
