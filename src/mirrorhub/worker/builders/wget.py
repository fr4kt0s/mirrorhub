"""wget builder for single files (keyrings, installer files, fallback fetches)."""

from __future__ import annotations

from pathlib import Path

from mirrorhub.models import Job, Repository

from .base import BuildError, BuildPlan, Step


def build(repo: Repository, job: Job, target: Path, cwd: Path) -> BuildPlan:
    if not repo.extra_files:
        raise BuildError("no extra files configured for this wget mirror")
    root = target.resolve()
    steps: list[Step] = []
    for item in repo.extra_files:
        url = item.get("url", "")
        relative = item.get("path", "")
        if not url or not relative:
            raise BuildError("extra file entries need both a URL and a relative path")
        destination = target / relative
        resolved = destination.resolve()
        if root not in resolved.parents:
            raise BuildError(f"extra file path escapes the repository directory: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        argv = ["wget", "--no-verbose", "--tries=3", "--timeout=60"]
        if repo.bwlimit_kbps:
            argv.append(f"--limit-rate={repo.bwlimit_kbps}k")
        argv.extend(repo.extra_args)
        if job.dry_run:
            argv.append("--spider")
        else:
            argv.extend(["--continue", "-O", str(destination)])
        argv.append(url)
        steps.append(Step(tuple(argv), f"fetch {relative}"))
    return BuildPlan(steps=steps, cwd=cwd, target=target)
