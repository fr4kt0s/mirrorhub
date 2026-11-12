"""Build validated sync plans from persistent job and repository data."""

from __future__ import annotations

from mirrorhub.enums import Method, Tool
from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, RsyncPlan, SyncPlan
from mirrorhub.worker.builders.base import resolve_paths


def build_ftpsync_plan(
    job: Job,
    repository: Repository,
    settings: Settings,
) -> FtpsyncPlan:
    """Create an FTPsync plan from a sync job and its repository."""
    if job.type != "sync":
        raise SyncConfigurationError("ftpsync plans can only be built for sync jobs")

    if repository.tool != Tool.FTPSYNC:
        raise SyncConfigurationError(
            f"repository tool {repository.tool} cannot use the ftpsync adapter"
        )

    if repository.method != Method.RSYNC:
        raise SyncConfigurationError(f"ftpsync requires the rsync method, got {repository.method}")

    target = resolve_paths(repository, job, settings)
    work_dir = settings.tmp_dir / "ftpsync" / repository.slug
    log_dir = work_dir / "log"

    try:
        return FtpsyncPlan(
            job_id=job.id or 0,
            repository_id=repository.id or 0,
            slug=repository.slug,
            adapter=Tool.FTPSYNC,
            source_protocol=repository.method,
            upstream_host=repository.upstream_host,
            upstream_root=repository.upstream_root,
            target=target,
            work_dir=work_dir,
            log_dir=log_dir,
            dry_run=job.dry_run,
            attempt=job.attempt,
            architectures=tuple(repository.architectures),
            exclude_patterns=tuple(repository.exclude_patterns),
            bwlimit_kbps=repository.bwlimit_kbps,
            extra_args=tuple(repository.extra_args),
        )
    except ValueError as exc:
        raise SyncConfigurationError(str(exc)) from exc


def build_rsync_plan(
    job: Job,
    repository: Repository,
    settings: Settings,
) -> RsyncPlan:
    """Create an rsync plan from a sync job and its repository."""
    if job.type != "sync":
        raise SyncConfigurationError("rsync plans can only be built for sync jobs")

    if repository.tool != Tool.RSYNC:
        raise SyncConfigurationError(
            f"repository tool {repository.tool} cannot use the rsync adapter"
        )

    if repository.method != Method.RSYNC:
        raise SyncConfigurationError(f"rsync requires the rsync method, got {repository.method}")

    target = resolve_paths(repository, job, settings)
    work_dir = settings.tmp_dir / "rsync" / repository.slug
    log_dir = work_dir / "log"

    try:
        return RsyncPlan(
            job_id=job.id or 0,
            repository_id=repository.id or 0,
            slug=repository.slug,
            adapter=Tool.RSYNC,
            source_protocol=repository.method,
            upstream_host=repository.upstream_host,
            upstream_root=repository.upstream_root,
            target=target,
            work_dir=work_dir,
            log_dir=log_dir,
            dry_run=job.dry_run,
            attempt=job.attempt,
            repository_kind=repository.kind,
            suites=tuple(repository.suites),
            architectures=tuple(repository.architectures),
            include_patterns=tuple(repository.include_patterns),
            exclude_patterns=tuple(repository.exclude_patterns),
            bwlimit_kbps=repository.bwlimit_kbps,
            extra_args=tuple(repository.extra_args),
        )
    except ValueError as exc:
        raise SyncConfigurationError(str(exc)) from exc


def build_sync_plan(
    job: Job,
    repository: Repository,
    settings: Settings,
) -> SyncPlan:
    """Dispatch plan construction to the adapter-specific factory."""
    match repository.tool:
        case Tool.FTPSYNC:
            return build_ftpsync_plan(job, repository, settings)
        case Tool.RSYNC:
            return build_rsync_plan(job, repository, settings)
        case _:
            raise SyncConfigurationError(f"no sync plan factory registered for {repository.tool}")
