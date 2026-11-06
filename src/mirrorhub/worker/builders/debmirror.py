"""Build debmirror commands with explicit architecture and keyring settings."""

from __future__ import annotations

from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.security.validators import validate_extra_args
from mirrorhub.settings import get_settings

from .base import BuildError, BuildPlan, Step


def build(
    repo: Repository,
    job: Job,
    target: Path,
    cwd: Path,
) -> BuildPlan:
    if not repo.architectures:
        raise BuildError("no architectures configured; use 'none' to mirror no architectures")
    if not repo.suites:
        raise BuildError("no suites or dists configured")
    if not repo.sections:
        raise BuildError("no sections or components configured")
    if repo.method not in ("http", "https", "rsync"):
        raise BuildError("unsupported debmirror transfer method")
    if repo.keyring_name != "trustedkeys.gpg":
        raise BuildError("only the managed trustedkeys.gpg keyring is supported")

    argument_error = validate_extra_args("debmirror", repo.extra_args)
    if argument_error:
        raise BuildError(argument_error)

    keyring = get_settings().gnupg_dir / "trustedkeys.gpg"

    argv: list[str] = [
        "debmirror",
        f"--method={repo.method}",
        f"--host={repo.upstream_host}",
        f"--root={repo.upstream_root}",
        f"--arch={','.join(repo.architectures)}",
        f"--dist={','.join(repo.suites)}",
        f"--section={','.join(repo.sections)}",
        "--source" if repo.include_source else "--nosource",
        f"--diff={repo.diff_mode}",
        "--progress",
        "--verbose",
    ]
    warnings: list[str] = []

    if repo.verify_signatures:
        argv.extend(
            [
                "--check-gpg",
                f"--keyring={keyring}",
            ]
        )
    else:
        argv.extend(["--no-check-gpg", "--ignore-release-gpg"])

    if repo.cleanup_mode == "none":
        argv.append("--nocleanup")
    elif repo.cleanup_mode == "cleanup":
        argv.append("--cleanup")
    elif repo.cleanup_mode != "post":
        raise BuildError("unsupported cleanup mode")

    rsync_extra = ",".join(repo.rsync_extra) if repo.rsync_extra else "none"
    argv.append(f"--rsync-extra={rsync_extra}")

    for pattern in repo.include_patterns:
        argv.append(f"--include={pattern}")
    for pattern in repo.exclude_patterns:
        argv.append(f"--exclude={pattern}")

    if repo.bwlimit_kbps:
        argv.append(f"--rsync-options=-aIL --partial --bwlimit={repo.bwlimit_kbps}")
        if repo.method != "rsync":
            warnings.append(
                "The bandwidth limit applies to rsync transfers only; "
                "HTTP and HTTPS downloads are not limited."
            )

    argv.extend(repo.extra_args)

    if job.dry_run:
        argv.append("--dry-run")

    argv.append(str(target))

    return BuildPlan(
        steps=[
            Step(
                tuple(argv),
                f"debmirror {repo.upstream_host}{repo.upstream_root}",
            )
        ],
        cwd=cwd,
        target=target,
        warnings=warnings,
    )
