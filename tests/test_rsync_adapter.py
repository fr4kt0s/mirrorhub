from datetime import UTC, datetime
from pathlib import Path

import pytest

from mirrorhub.enums import Tool
from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings
from mirrorhub.sync.adapters import RsyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, RsyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncStatus
from mirrorhub.worker.runner import RunResult

_STARTED_AT = datetime(2024, 1, 1, tzinfo=UTC)
_FINISHED_AT = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)


def _plan(**overrides: object) -> RsyncPlan:
    values = {
        "job_id": 1,
        "repository_id": 2,
        "slug": "example",
        "adapter": Tool.RSYNC,
        "source_protocol": "rsync",
        "upstream_host": "rsync.example.org",
        "upstream_root": "/module",
        "target": Path("/data/example"),
        "work_dir": Path("/tmp/example"),
        "log_dir": Path("/tmp/example/log"),
        "repository_kind": "generic",
    }
    values.update(overrides)
    return RsyncPlan(**values)


def _context(plan: RsyncPlan) -> SyncExecutionContext:
    settings = Settings(state_dir=Path("/tmp"))
    job = Job(
        id=1,
        repo_id=2,
        tool=Tool.RSYNC.value,
        dry_run=plan.dry_run,
    )
    repository = Repository(
        id=2,
        slug=plan.slug,
        name=plan.slug,
        kind=plan.repository_kind,
        tool=Tool.RSYNC.value,
        upstream_host=plan.upstream_host,
        upstream_root=plan.upstream_root,
        method=plan.source_protocol,
        suites=list(plan.suites),
        architectures=list(plan.architectures),
        include_patterns=list(plan.include_patterns),
        exclude_patterns=list(plan.exclude_patterns),
        bwlimit_kbps=plan.bwlimit_kbps,
        extra_args=list(plan.extra_args),
    )
    return SyncExecutionContext(
        settings=settings,
        job=job,
        repository=repository,
        target=plan.target,
        work_dir=plan.work_dir,
        log_dir=plan.log_dir,
    )


def test_rsync_adapter_rejects_foreign_plan() -> None:
    plan = FtpsyncPlan(
        job_id=1,
        repository_id=2,
        slug="debian",
        adapter=Tool.FTPSYNC,
        source_protocol="rsync",
        upstream_host="deb.debian.org",
        upstream_root="debian",
        target=Path("/data/debian"),
        work_dir=Path("/tmp/debian"),
        log_dir=Path("/tmp/debian/log"),
        architectures=("amd64",),
    )

    with pytest.raises(SyncConfigurationError):
        RsyncAdapter().validate_plan(plan)


def test_rsync_adapter_builds_generic_plan() -> None:
    plan = _plan()
    built = RsyncAdapter().build_plan(plan, _context(plan))

    assert len(built.steps) == 1
    argv = list(built.steps[0].argv)
    assert "--delete-after" in argv
    assert argv[-2] == "rsync://rsync.example.org/module/"
    assert argv[-1] == "/data/example/"


def test_rsync_adapter_builds_arch_plan() -> None:
    plan = _plan(
        repository_kind="arch",
        suites=("core",),
        architectures=("x86_64",),
    )
    built = RsyncAdapter().build_plan(plan, _context(plan))

    assert len(built.steps) == 2
    assert built.steps[0].label == "Arch pool and repository links"
    assert built.steps[1].label == "Arch databases"
    assert "--delay-updates" in built.steps[1].argv


def test_rsync_adapter_interprets_success() -> None:
    plan = _plan()
    result = RsyncAdapter().interpret_result(
        plan,
        RunResult(exit_code=0, cancelled=False),
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )

    assert result.status == SyncStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.error_code is None


def test_rsync_adapter_interprets_failure() -> None:
    plan = _plan()
    result = RsyncAdapter().interpret_result(
        plan,
        RunResult(exit_code=23, cancelled=False),
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )

    assert result.status == SyncStatus.FAILED
    assert result.exit_code == 23
    assert result.error_code == SyncErrorCode.UNKNOWN
    assert result.retryable is True
    assert result.message == "rsync exited 23"


def test_rsync_adapter_interprets_cancellation() -> None:
    plan = _plan()
    result = RsyncAdapter().interpret_result(
        plan,
        RunResult(exit_code=130, cancelled=True),
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )

    assert result.status == SyncStatus.CANCELLED
    assert result.error_code == SyncErrorCode.CANCELLED
    assert result.retryable is False
