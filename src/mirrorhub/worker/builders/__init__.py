"""Command builders. Pure functions: repository config in, argument lists out.

No shell strings anywhere: every step is a list passed straight to exec.
`base` is imported before the submodules so its types exist before they are needed.
"""

from __future__ import annotations

from mirrorhub.enums import Tool
from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings

from . import debmirror, rsync, wget
from .base import BuildError, BuildPlan, Step, resolve_paths

__all__ = ["BuildError", "BuildPlan", "Step", "build", "resolve_paths"]


def build(repo: Repository, job: Job, settings: Settings) -> BuildPlan:
    target = resolve_paths(repo, job, settings)
    if repo.tool == Tool.DEBMIRROR.value:
        return debmirror.build(repo, job, target, target)
    if repo.tool == Tool.RSYNC.value:
        return rsync.build(repo, job, target, target)
    if repo.tool == Tool.WGET.value:
        return wget.build(repo, job, target, target)
    raise BuildError(f"unsupported tool for sync jobs: {repo.tool}")
