from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters import DebmirrorAdapter
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import DebmirrorPlan, FtpsyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncStatus


def _plan(**overrides: object) -> DebmirrorPlan:
    values = {
        "job_id": 1,
        "repository_id": 2,
        "slug": "ubuntu",
        "adapter": Tool.DEBMIRROR,
        "source_protocol": "https",
        "upstream_host": "archive.ubuntu.com",
        "upstream_root": "/ubuntu",
        "target": Path("/data/ubuntu"),
        "work_dir": Path("/tmp/ubuntu"),
        "log_dir": Path("/tmp/ubuntu/log"),
        "architectures": ("amd64",),
        "suites": ("noble",),
        "sections": ("main",),
        "diff_mode": "rsync",
        "cleanup_mode": "post",
    }
    values.update(overrides)
    return DebmirrorPlan(**values)


def _context(plan: DebmirrorPlan) -> SimpleNamespace:
    job = SimpleNamespace(dry_run=plan.dry_run)
    repository = SimpleNamespace(
        upstream_host=plan.upstream_host,
        upstream_root=plan.upstream_root,
        method=plan.source_protocol,
        architectures=plan.architectures,
        suites=plan.suites,
        sections=plan.sections,
        include_source=plan.include_source,
        diff_mode=plan.diff_mode,
        verify_signatures=plan.verify_signatures,
        cleanup_mode=plan.cleanup_mode,
        rsync_extra=plan.rsync_extra,
        include_patterns=plan.include_patterns,
        exclude_patterns=plan.exclude_patterns,
        bwlimit_kbps=plan.bwlimit_kbps,
        extra_args=plan.extra_args,
        keyring_name="trustedkeys.gpg",
    )
    return SimpleNamespace(
        job=job,
        repository=repository,
        target=plan.target,
        work_dir=plan.work_dir,
        log_dir=plan.log_dir,
        settings=SimpleNamespace(tmp_dir=Path("/tmp"), gnupg_dir=Path("/tmp/gnupg")),
    )


def test_debmirror_adapter_rejects_foreign_plan() -> None:
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
        DebmirrorAdapter().validate_plan(plan)


def test_debmirror_adapter_builds_https_plan() -> None:
    plan = _plan()
    built = DebmirrorAdapter().build_plan(plan, _context(plan))

    assert len(built.steps) == 1
    argv = list(built.steps[0].argv)
    assert argv[0] == "debmirror"
    assert "--method=https" in argv
    assert "--host=archive.ubuntu.com" in argv
    assert "--root=/ubuntu" in argv
    assert "--arch=amd64" in argv
    assert "--dist=noble" in argv
    assert "--section=main" in argv
    assert "--nosource" in argv
    assert "--no-check-gpg" in argv
    assert argv[-1] == "/data/ubuntu"


def test_debmirror_adapter_builds_signature_verification() -> None:
    plan = _plan(verify_signatures=True)
    built = DebmirrorAdapter().build_plan(plan, _context(plan))

    argv = list(built.steps[0].argv)
    assert "--check-gpg" in argv
    assert "--keyring=/tmp/gnupg/trustedkeys.gpg" in argv
    assert "--no-check-gpg" not in argv


def test_debmirror_adapter_builds_dry_run() -> None:
    plan = _plan(dry_run=True)
    built = DebmirrorAdapter().build_plan(plan, _context(plan))

    assert "--dry-run" in built.steps[0].argv


def test_debmirror_adapter_interprets_success() -> None:
    plan = _plan()
    result = DebmirrorAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=False, exit_code=0),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.error_code is None


def test_debmirror_adapter_interprets_failure() -> None:
    plan = _plan()
    result = DebmirrorAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=False, exit_code=2),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.FAILED
    assert result.exit_code == 2
    assert result.error_code == SyncErrorCode.UNKNOWN
    assert result.retryable is True
    assert result.message == "debmirror exited 2"


def test_debmirror_adapter_interprets_cancellation() -> None:
    plan = _plan()
    result = DebmirrorAdapter().interpret_result(
        plan,
        SimpleNamespace(cancelled=True, exit_code=130),
        started_at=1,
        finished_at=2,
    )

    assert result.status == SyncStatus.CANCELLED
    assert result.error_code == SyncErrorCode.CANCELLED
    assert result.retryable is False
