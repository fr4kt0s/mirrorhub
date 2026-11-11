"""Build rsync plans for generic trees and pool-based Arch mirrors."""

from __future__ import annotations

from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.security import validators

from .base import BuildError, BuildPlan, Step

BASE_FLAGS = ("rsync", "-aH", "--numeric-ids", "--safe-links", "--no-motd", "--info=progress2")
DB_PATTERNS = (
    "*.db",
    "*.db.sig",
    "*.db.tar.*",
    "*.files",
    "*.files.sig",
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
    parts = [part for part in (root.strip("/"), suffix.strip("/")) if part]
    return "rsync://" + host + "/" + "/".join(parts) + "/"


def build(repo: Repository, job: Job, target: Path, cwd: Path) -> BuildPlan:
    error = validators.validate_host(repo.upstream_host) or validators.validate_root(
        repo.upstream_root
    )
    if error:
        raise BuildError(error)
    if not repo.upstream_root.strip("/"):
        raise BuildError("rsync source requires a module, for example /packages")
    if repo.method != "rsync":
        raise BuildError("rsync tool requires method rsync")
    error = validators.validate_extra_args("rsync", repo.extra_args)
    if error:
        raise BuildError(error)
    source = _source_url(repo.upstream_host, repo.upstream_root)
    common = _common(repo, dry_run=job.dry_run)

    if repo.kind != "arch":
        argv = [*common, "--delete-after", source, f"{target}/"]
        return BuildPlan(
            steps=[Step(tuple(argv), f"rsync {repo.upstream_host}{repo.upstream_root}")],
            cwd=cwd,
            target=target,
        )

    if not repo.suites:
        raise BuildError("no Arch repositories configured (core, extra, ...)")
    if not repo.architectures or "none" in repo.architectures:
        raise BuildError("Arch requires concrete architectures")
    error = validators.validate_architectures(repo.architectures)
    if error:
        raise BuildError(error)
    error = validators.validate_list_field("Arch repositories", repo.suites)
    if error:
        raise BuildError(error)
    if any("/" in name or name in (".", "..", "pool") for name in repo.suites):
        raise BuildError("Arch repository names must be single directory names, not pool")
    if repo.include_patterns or repo.exclude_patterns:
        raise BuildError("custom filters are not supported for pool-based Arch sync")

    tree_rules = ["--include=/pool/", "--include=/pool/packages/***"]
    database_rules: list[str] = []
    for name in sorted(set(repo.suites)):
        parents = [f"--include=/{name}/", f"--include=/{name}/os/"]
        tree_rules.extend(parents)
        database_rules.extend(parents)
        for arch in sorted(set(repo.architectures)):
            prefix = f"/{name}/os/{arch}"
            tree_rules.append(f"--include={prefix}/***")
            database_rules.append(f"--include={prefix}/")
            database_rules.extend(f"--include={prefix}/{pattern}" for pattern in DB_PATTERNS)
    tree_rules.append("--exclude=*")
    database_rules.append("--exclude=*")

    packages = [
        *common,
        *[f"--exclude={pattern}" for pattern in DB_PATTERNS],
        *tree_rules,
        source,
        f"{target}/",
    ]
    databases = [
        *common,
        "--delay-updates",
        *database_rules,
        source,
        f"{target}/",
    ]
    return BuildPlan(
        steps=[
            Step(tuple(packages), "Arch pool and repository links"),
            Step(tuple(databases), "Arch databases"),
        ],
        cwd=cwd,
        target=target,
        warnings=[
            "Arch sync downloads the entire pool/packages directory.",
            "Arch deletion is disabled pending safe pool garbage collection.",
            "Two-pass sync is not an atomic snapshot; an upstream change may require a retry.",
        ],
    )
