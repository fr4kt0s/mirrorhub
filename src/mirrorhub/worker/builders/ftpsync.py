"""ftpsync builder for Debian-style archive mirrors."""

from __future__ import annotations

import shlex
from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.security import validators
from mirrorhub.settings import Settings

from .base import BuildError, BuildPlan, Step


def _config(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}\n"


def build(
    repo: Repository,
    job: Job,
    target: Path,
    cwd: Path,
    settings: Settings,
) -> BuildPlan:
    if job.dry_run:
        raise BuildError("ftpsync does not support dry runs")

    if repo.method != "rsync":
        raise BuildError("ftpsync requires the rsync method")

    error = validators.validate_host(repo.upstream_host) or validators.validate_root(
        repo.upstream_root
    )
    if error:
        raise BuildError(error)

    if not repo.architectures or "none" in repo.architectures:
        raise BuildError("ftpsync requires at least one architecture")

    error = validators.validate_extra_args("ftpsync", repo.extra_args)
    if error:
        raise BuildError(error)

    runtime = settings.tmp_dir / "ftpsync" / repo.slug
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "log").mkdir(parents=True, exist_ok=True)

    config_dir = Path.home() / ".config" / "ftpsync"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"ftpsync-{repo.slug}.conf"

    config_path.write_text(
        "".join(
            [
                _config("MIRRORNAME", repo.slug),
                _config("TO", f"{target}/"),
                _config("LOGDIR", str(runtime / "log")),
                _config("MAILTO", ""),
                _config("HUB", "false"),
                _config("RSYNC_HOST", repo.upstream_host),
                _config("RSYNC_PATH", repo.upstream_root.strip("/")),
                _config("ARCH_INCLUDE", " ".join(repo.architectures)),
                _config(
                    "EXCLUDE",
                    " ".join(
                        f"--exclude={pattern}" for pattern in repo.exclude_patterns
                    ),
                ),
                _config("RSYNC_BW", str(repo.bwlimit_kbps)),
            ]
        ),
        encoding="utf-8",
    )

    argv = [
        "env",
        "LOGNAME=mirror",
        "ftpsync",
        f"sync:archive:{repo.slug}",
    ]

    return BuildPlan(
        steps=[
            Step(
                tuple(argv),
                f"ftpsync {repo.upstream_host}/{repo.upstream_root}",
            )
        ],
        cwd=cwd,
        target=target,
    )
