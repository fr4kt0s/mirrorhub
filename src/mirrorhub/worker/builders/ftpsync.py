"""ftpsync builder for Debian-style archive mirrors."""

from __future__ import annotations

import shlex
from pathlib import Path

from mirrorhub.models import Job, Repository
from mirrorhub.security import validators
from mirrorhub.services import logs
from mirrorhub.settings import Settings

from .base import BuildError, BuildPlan, FollowedLog, Step

# rsync lines copied into the job log; the complete file is kept for download
RSYNC_LIVE_LINES = 500


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
        raise BuildError("ftpsync supports either ARCH_INCLUDE or ARCH_EXCLUDE, not both")

    error = validators.validate_extra_args("ftpsync", repo.extra_args)
    if error:
        raise BuildError(error)

    runtime = settings.tmp_dir / "ftpsync" / repo.slug
    runtime.mkdir(parents=True, exist_ok=True)
    log_dir = logs.ftpsync_log_dir(repo.slug, settings)
    log_dir.mkdir(parents=True, exist_ok=True)

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
                _config("LOGDIR", str(log_dir)),
                _config("MAILTO", ""),
                _config("HUB", "false"),
                _config("RSYNC_HOST", repo.upstream_host),
                _config("RSYNC_PATH", repo.upstream_root.strip("/")),
                architecture_config,
                _config(
                    "EXCLUDE",
                    " ".join(f"--exclude={pattern}" for pattern in repo.exclude_patterns),
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

    # ftpsync writes its protocol and the rsync output to files in LOGDIR, not to
    # stdout (names: ftpsync-<archive>.log, rsync-ftpsync-<archive>.log and
    # .error or .error.log depending on the version); the runner copies them into
    # the job log and keeps the complete rsync log with the job.
    name = f"ftpsync-{repo.slug}"
    follow = (
        FollowedLog(
            (logs.ftpsync_rsync_log(repo.slug, settings),),
            "[rsync]",
            live_lines=RSYNC_LIVE_LINES,
            archive_as=logs.RSYNC_LOG,
        ),
        FollowedLog(
            (log_dir / f"rsync-{name}.error", log_dir / f"rsync-{name}.error.log"),
            "[rsync error]",
        ),
        FollowedLog((log_dir / f"{name}.log",), "[ftpsync]"),
    )

    return BuildPlan(
        steps=[
            Step(
                tuple(argv),
                f"ftpsync {repo.upstream_host}/{repo.upstream_root}",
                follow,
            )
        ],
        cwd=cwd,
        target=target,
    )
