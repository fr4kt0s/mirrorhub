from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters import WgetAdapter
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, WgetPlan
from mirrorhub.sync.result import SyncErrorCode, SyncStatus


def _plan(**overrides: object) -> WgetPlan:
    values = {
        "job_id": 1,
        "repository_id": 2,
        "slug": "example-tree",
        "adapter": Tool.WGET,
        "source_protocol": "https",
        "upstream_host": "example.org",
        "upstream_root": "/linux/dists",
        "target": Path("/data/example-tree"),
        "work_dir": Path("/tmp/example-tree"),
        "log_dir": Path("/tmp/example-tree/log"),
    }
    values.update(overrides)
    return WgetPlan(**values)


def _context(plan: WgetPlan) -> SimpleNamespace:
    job = SimpleNamespace(dry_run=plan.dry_run)
    repository = SimpleNamespace(
        upstream_host=plan.upstream_host,
        upstream_root=plan.upstream_root,
        method=plan.source_protocol,
        include_regex=plan.include_regex,
        exclude_regex=plan.exclude_regex,
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


def test_wget_adapter_rejects_foreign_plan() -> None:
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
        WgetAdapter().validate_plan(plan)


def test_wget_adapter_builds_https_tree_plan() -> None:
    plan = _plan()
    built = WgetAdapter().build_plan(plan, _context(plan))

    assert len(built.steps) == 1
    argv = list(built.steps[0].argv)
    assert argv[0] == "wget"
    assert "--recursive" in argv
    assert "--no-parent" in argv
    assert "--no-host-directories" in argv
    assert "--cut-dirs=2" in argv
    assert "--directory-prefix=/data/example-tree" in argv
    assert "--regex-type=posix" not in argv
    assert argv[-1] == "https://example.org/linux/dists/"


def test_wget_adapter_builds_regex_filters() -> None:
    plan = _plan(
        include_regex=r"\\.iso$",
        exclude_regex=r"/archive/",
    )
    built = WgetAdapter().build_plan(plan, _context(plan))

    argv = list(built.steps[0].argv)
    assert "--regex-type=posix" in argv
    assert "--accept-regex" in argv
    assert r"\\.iso$" in argv
    assert "--reject-regex" in argv
    assert "/archive/" in argv


def test_wget_adapter_builds_bandwidth_limit() -> None:
    plan = _plan(bwlimit_kbps=2048)
    built = WgetAdapter().build_plan(plan, _context(plan))

    assert "--limit-rate=2048k" in built.steps[0].argv


def test_wget_adapter_builds_dry_run() -> None:
    plan = _plan(dry_run=True)
    built = WgetAdapter().build_plan(plan, _context(plan))

    assert "--spider" in built.steps[0].argv


def test_wget_adapter_interprets_success() -> None:
    plan = _plan()
    result = WgetAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=False, exit_code=0),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.error_code is None


def test_wget_adapter_interprets_failure() -> None:
    plan = _plan()
    result = WgetAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=False, exit_code=8),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.FAILED
    assert result.exit_code == 8
    assert result.error_code == SyncErrorCode.UNKNOWN
    assert result.retryable is True
    assert result.message == "wget exited 8"


def test_wget_adapter_interprets_cancellation() -> None:
    plan = _plan()
    result = WgetAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=True, exit_code=130),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.CANCELLED
    assert result.error_code == SyncErrorCode.CANCELLED
    assert result.retryable is False
