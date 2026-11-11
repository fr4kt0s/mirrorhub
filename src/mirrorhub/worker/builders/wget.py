"""wget builder for complete HTTP directory-tree mirrors."""

from __future__ import annotations

from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.security import validators

from .base import BuildError, BuildPlan, Step


def build(repo: Repository, job: Job, target: Path, cwd: Path) -> BuildPlan:
    error = validators.validate_host(repo.upstream_host) or validators.validate_root(
        repo.upstream_root
    )
    if error:
        raise BuildError(error)
    if repo.method not in ("http", "https"):
        raise BuildError("wget supports http and https only")
    error = validators.validate_extra_args("wget", repo.extra_args)
    if error:
        raise BuildError(error)

    root = repo.upstream_root.strip("/")
    depth = 0 if not root else len(root.split("/"))
    url = f"{repo.method}://{repo.upstream_host}/{root}/" if root else f"{repo.method}://{repo.upstream_host}/"

    argv = [
        "wget",
        "--recursive",
        "--no-parent",
        "--no-host-directories",
        f"--cut-dirs={depth}",
        f"--directory-prefix={target}",
        "--no-verbose",
        "--tries=3",
        "--timeout=60",
    ]
    if repo.include_regex or repo.exclude_regex:
        argv.append("--regex-type=posix")
    if repo.include_regex:
        argv.extend(["--accept-regex", repo.include_regex])
    if repo.exclude_regex:
        argv.extend(["--reject-regex", repo.exclude_regex])
    if repo.bwlimit_kbps:
        argv.append(f"--limit-rate={repo.bwlimit_kbps}k")
    argv.extend(repo.extra_args)
    if job.dry_run:
        argv.append("--spider")
    argv.append(url)

    return BuildPlan(
        steps=[Step(tuple(argv), f"mirror tree {repo.upstream_host}{repo.upstream_root}")],
        cwd=cwd,
        target=target,
    )
