"""rsync argument builder for Arch Linux and generic rsync mirrors.

Arch: two passes per repository. Packages first (databases excluded), then the databases
themselves with --delay-updates so partial database files never appear under their final
names. Databases therefore land last, after the packages they reference.
"""

from __future__ import annotations

from pathlib import Path

from mirrorhub.models import Job, Repository

from .base import BuildError, BuildPlan, Step

BASE_FLAGS = ("rsync", "-aH", "--numeric-ids", "--safe-links", "--no-motd", "--info=progress2")

DB_PATTERNS = (
    "*.db",
    "*.db.sig",
    "*.db.tar.*",
    "*.files",
    "*.files.tar.*",
    "*.abs.tar.*",
    "*.links.tar.*",
)


def _common(repo: Repository, *, dry_run: bool) -> list[str]:
    argv = list(BASE_FLAGS)
    if dry_run:
        argv.insert(1, "--dry-run")
    if repo.bwlimit_kbps:
        argv.append(f"--bwlimit={repo.bwlimit_kbps}")
    for pattern in repo.include_patterns:
        argv.append(f"--include={pattern}")
    for pattern in repo.exclude_patterns:
        argv.append(f"--exclude={pattern}")
    argv.extend(repo.extra_args)
    return argv


def _source_url(host: str, root: str, suffix: str = "") -> str:
    path = root.strip("/")
    parts = [part for part in (path, suffix.strip("/")) if part]
    return "rsync://" + host + "/" + "/".join(parts) + "/"


def build(repo: Repository, job: Job, target: Path, cwd: Path) -> BuildPlan:
    steps: list[Step] = []

    if repo.kind == "arch":
        if not repo.suites:
            raise BuildError("no Arch repositories configured (core, extra, ...)")
        if not repo.architectures:
            raise BuildError("no architectures configured for the Arch mirror")
        for name in repo.suites:
            for arch in repo.architectures:
                dest = target / name / "os" / arch
                dest.mkdir(parents=True, exist_ok=True)
                source = _source_url(repo.upstream_host, repo.upstream_root, f"{name}/os/{arch}")
                packages = [
                    *_common(repo, dry_run=job.dry_run),
                    "--delete-after",
                    *[f"--exclude={pattern}" for pattern in DB_PATTERNS],
                    source,
                    f"{dest}/",
                ]
                databases = [
                    *_common(repo, dry_run=job.dry_run),
                    "--delete",
                    "--delay-updates",
                    *[f"--include={pattern}" for pattern in DB_PATTERNS],
                    "--exclude=*",
                    source,
                    f"{dest}/",
                ]
                steps.append(Step(tuple(packages), f"packages {name}/{arch}"))
                steps.append(Step(tuple(databases), f"databases {name}/{arch}"))
        return BuildPlan(steps=steps, cwd=cwd, target=target)

    argv = [
        *_common(repo, dry_run=job.dry_run),
        "--delete-after",
        _source_url(repo.upstream_host, repo.upstream_root),
        f"{target}/",
    ]
    steps.append(Step(tuple(argv), f"rsync {repo.upstream_host}{repo.upstream_root}"))
    return BuildPlan(steps=steps, cwd=cwd, target=target)
