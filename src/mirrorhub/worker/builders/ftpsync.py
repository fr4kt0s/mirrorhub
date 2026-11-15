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

    if repo.architectures and repo.arch_exclude:
        raise BuildError(
            "ftpsync supports either ARCH_INCLUDE or ARCH_EXCLUDE, not both"
        )

    error = validators.validate_extra_args("ftpsync", repo.extra_args)
    if error:
        raise BuildError(error)

    runtime = settings.tmp_dir / "ftpsync" / repo.slug
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "log").mkdir(parents=True, exist_ok=True)

    config_dir = Path.home() / ".config" / "ftpsync"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"ftpsync-{repo.slug}.conf"

    if repo.arch_exclude:
        architecture_config = _config("ARCH_EXCLUDE", " ".join(repo.arch_exclude))
    elif repo.architectures:
        architecture_config = _config("ARCH_INCLUDE", " ".join(repo.architectures))
    else:
        architecture_config = ""

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
                architecture_config,
                _config(
                    "EXCLUDE",
                    " ".join(
                        f"--exclude={pattern}" for pattern in repo.exclude_patterns
                    ),
                ),
                _config("RSYNC_EXTRA", " ".join(repo.rsync_extra)),
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
