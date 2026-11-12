from pathlib import Path

import pytest
from pydantic import ValidationError

from mirrorhub.enums import Tool
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import DebmirrorPlan, FtpsyncPlan, RsyncPlan, WgetPlan


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


def _rsync_plan_kwargs() -> dict:
    return {
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


def _debmirror_plan_kwargs() -> dict:
    return {
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


def _wget_plan_kwargs() -> dict:
    return {
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


def test_rsync_plan_accepts_generic_configuration() -> None:
    plan = RsyncPlan(**_rsync_plan_kwargs())

    assert plan.adapter == Tool.RSYNC
    assert plan.source_protocol == "rsync"
    assert plan.repository_kind == "generic"
    assert plan.suites == ()
    assert plan.architectures == ()
    assert plan.bwlimit_kbps == 0


def test_rsync_plan_accepts_arch_configuration() -> None:
    kwargs = _rsync_plan_kwargs()
    kwargs.update(
        {
            "repository_kind": "arch",
            "suites": ("core", "extra"),
            "architectures": ("x86_64",),
        }
    )

    plan = RsyncPlan(**kwargs)

    assert plan.repository_kind == "arch"
    assert plan.suites == ("core", "extra")
    assert plan.architectures == ("x86_64",)


def test_rsync_plan_rejects_unsupported_source_protocol() -> None:
    kwargs = _rsync_plan_kwargs()
    kwargs["source_protocol"] = "https"

    with pytest.raises(ValidationError):
        RsyncPlan(**kwargs)


def test_rsync_plan_rejects_negative_bandwidth_limit() -> None:
    kwargs = _rsync_plan_kwargs()
    kwargs["bwlimit_kbps"] = -1

    with pytest.raises(ValidationError):
        RsyncPlan(**kwargs)


def test_debmirror_plan_accepts_valid_configuration() -> None:
    plan = DebmirrorPlan(**_debmirror_plan_kwargs())

    assert plan.adapter == Tool.DEBMIRROR
    assert plan.source_protocol == "https"
    assert plan.architectures == ("amd64",)
    assert plan.suites == ("noble",)
    assert plan.sections == ("main",)
    assert plan.include_source is False
    assert plan.verify_signatures is False


def test_debmirror_plan_requires_architecture() -> None:
    kwargs = _debmirror_plan_kwargs()
    kwargs["architectures"] = ()

    with pytest.raises(ValidationError):
        DebmirrorPlan(**kwargs)


def test_debmirror_plan_requires_suite() -> None:
    kwargs = _debmirror_plan_kwargs()
    kwargs["suites"] = ()

    with pytest.raises(ValidationError):
        DebmirrorPlan(**kwargs)


def test_debmirror_plan_requires_section() -> None:
    kwargs = _debmirror_plan_kwargs()
    kwargs["sections"] = ()

    with pytest.raises(ValidationError):
        DebmirrorPlan(**kwargs)


def test_debmirror_plan_rejects_unsupported_source_protocol() -> None:
    kwargs = _debmirror_plan_kwargs()
    kwargs["source_protocol"] = "ftp"

    with pytest.raises(ValidationError):
        DebmirrorPlan(**kwargs)


def test_wget_plan_accepts_valid_configuration() -> None:
    plan = WgetPlan(**_wget_plan_kwargs())

    assert plan.adapter == Tool.WGET
    assert plan.source_protocol == "https"
    assert plan.include_regex == ""
    assert plan.exclude_regex == ""
    assert plan.bwlimit_kbps == 0
    assert plan.extra_args == ()


def test_wget_plan_accepts_filters_and_bandwidth_limit() -> None:
    kwargs = _wget_plan_kwargs()
    kwargs.update(
        {
            "include_regex": r"\\.iso$",
            "exclude_regex": r"/archive/",
            "bwlimit_kbps": 2048,
            "extra_args": ("--no-check-certificate",),
        }
    )

    plan = WgetPlan(**kwargs)

    assert plan.include_regex == r"\\.iso$"
    assert plan.exclude_regex == r"/archive/"
    assert plan.bwlimit_kbps == 2048
    assert plan.extra_args == ("--no-check-certificate",)


def test_wget_plan_rejects_unsupported_source_protocol() -> None:
    kwargs = _wget_plan_kwargs()
    kwargs["source_protocol"] = "rsync"

    with pytest.raises(ValidationError):
        WgetPlan(**kwargs)


def test_wget_plan_rejects_negative_bandwidth_limit() -> None:
    kwargs = _wget_plan_kwargs()
    kwargs["bwlimit_kbps"] = -1

    with pytest.raises(ValidationError):
        WgetPlan(**kwargs)


def test_sync_configuration_error_has_config_code() -> None:
    error = SyncConfigurationError("invalid configuration")

    assert error.code == "config_invalid"
