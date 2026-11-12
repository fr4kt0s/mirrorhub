from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters import RsyncAdapter
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, RsyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncStatus


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


def _context(plan: RsyncPlan) -> SimpleNamespace:
    job = SimpleNamespace(dry_run=plan.dry_run)
    repository = SimpleNamespace(
        upstream_host=plan.upstream_host,
        upstream_root=plan.upstream_root,
        method=plan.source_protocol,
        kind=plan.repository_kind,
        suites=plan.suites,
        architectures=plan.architectures,
        include_patterns=plan.include_patterns,
        exclude_patterns=plan.exclude_patterns,
        bwlimit_kbps=plan.bwlimit_kbps,
        extra_args=plan.extra_args,
    )
    return SimpleNamespace(
        job=job,
        repository=repository,
        target=plan.target,
        work_dir=plan.work_dir,
        log_dir=plan.log_dir,
        settings=SimpleNamespace(tmp_dir=Path("/tmp")),
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
        SimpleNamespace(cancelled=False, exit_code=0),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.error_code is None


def test_rsync_adapter_interprets_failure() -> None:
    plan = _plan()
    result = RsyncAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=False, exit_code=23),
        started_at=1,
        finished_at=2,
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
        SimpleNamespace(cancelled=True, exit_code=130),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.CANCELLED
    assert result.error_code == SyncErrorCode.CANCELLED
    assert result.retryable is False
