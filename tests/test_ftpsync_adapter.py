from datetime import UTC, datetime
from pathlib import Path

import pytest

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters import FtpsyncAdapter
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncStatus
from mirrorhub.worker.runner import RunResult


def _plan() -> FtpsyncPlan:
    return FtpsyncPlan(
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


def test_adapter_rejects_foreign_plan() -> None:
    adapter = FtpsyncAdapter()

    with pytest.raises(SyncConfigurationError):
        adapter.validate_plan(object())  # type: ignore[arg-type]


def test_adapter_rejects_dry_run() -> None:
    adapter = FtpsyncAdapter()
    plan = _plan().model_copy(update={"dry_run": True})

    with pytest.raises(SyncConfigurationError):
        adapter.validate_plan(plan)


def test_adapter_rejects_empty_architectures() -> None:
    adapter = FtpsyncAdapter()
    plan = _plan().model_copy(update={"architectures": ()})

    with pytest.raises(SyncConfigurationError):
        adapter.validate_plan(plan)


def test_adapter_maps_successful_result() -> None:
    adapter = FtpsyncAdapter()
    started = datetime.now(UTC)
    finished = datetime.now(UTC)
    result = RunResult(exit_code=0, cancelled=False)

    mapped = adapter.interpret_result(_plan(), result, started, finished)

    assert mapped.status == SyncStatus.SUCCEEDED
    assert mapped.exit_code == 0
    assert mapped.error_code is None


def test_adapter_maps_cancelled_result() -> None:
    adapter = FtpsyncAdapter()
    started = datetime.now(UTC)
    finished = datetime.now(UTC)
    result = RunResult(exit_code=143, cancelled=True)

    mapped = adapter.interpret_result(_plan(), result, started, finished)

    assert mapped.status == SyncStatus.CANCELLED
    assert mapped.error_code == SyncErrorCode.CANCELLED
    assert not mapped.retryable
