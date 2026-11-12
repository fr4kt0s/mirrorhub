from pathlib import Path

import pytest
from pydantic import ValidationError

from mirrorhub.enums import Tool
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, RsyncPlan


def _ftpsync_plan_kwargs() -> dict:
    return {
        "job_id": 1,
        "repository_id": 2,
        "slug": "debian",
        "adapter": Tool.FTPSYNC,
        "source_protocol": "rsync",
        "upstream_host": "deb.debian.org",
        "upstream_root": "debian",
        "target": Path("/data/debian"),
        "work_dir": Path("/tmp/debian"),
        "log_dir": Path("/tmp/debian/log"),
        "architectures": ("amd64", "arm64"),
    }


def test_ftpsync_plan_accepts_valid_configuration() -> None:
    plan = FtpsyncPlan(**_ftpsync_plan_kwargs())

    assert plan.adapter == Tool.FTPSYNC
    assert plan.source_protocol == "rsync"
    assert plan.architectures == ("amd64", "arm64")
    assert plan.bwlimit_kbps == 0


def test_ftpsync_plan_requires_at_least_one_architecture() -> None:
    kwargs = _ftpsync_plan_kwargs()
    kwargs["architectures"] = ()

    with pytest.raises(ValidationError):
        FtpsyncPlan(**kwargs)


def test_ftpsync_plan_rejects_unsupported_source_protocol() -> None:
    kwargs = _ftpsync_plan_kwargs()
    kwargs["source_protocol"] = "https"

    with pytest.raises(ValidationError):
        FtpsyncPlan(**kwargs)


def test_rsync_plan_rejects_negative_bandwidth_limit() -> None:
    with pytest.raises(ValidationError):
        RsyncPlan(
            job_id=1,
            repository_id=2,
            slug="example",
            adapter=Tool.RSYNC,
            source_protocol="rsync",
            upstream_host="rsync.example.org",
            upstream_root="module",
            target=Path("/data/example"),
            work_dir=Path("/tmp/example"),
            log_dir=Path("/tmp/example/log"),
            bwlimit_kbps=-1,
        )


def test_sync_configuration_error_has_config_code() -> None:
    error = SyncConfigurationError("invalid configuration")

    assert error.code == "config_invalid"
