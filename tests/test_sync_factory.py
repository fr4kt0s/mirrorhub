from pathlib import Path
from typing import Any, cast

import pytest

from mirrorhub.enums import Method, Tool
from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.factory import build_ftpsync_plan, build_sync_plan
from mirrorhub.sync.plan import FtpsyncPlan


class _Settings:
    def __init__(self, root: Path) -> None:
        self.data_dir = root / "data"
        self.tmp_dir = root / "tmp"


def _repository(**overrides: Any) -> Repository:
    values: dict[str, Any] = {
        "slug": "debian",
        "name": "Debian",
        "tool": Tool.FTPSYNC,
        "method": Method.RSYNC,
        "upstream_host": "deb.debian.org",
        "upstream_root": "debian",
        "architectures": ["amd64", "arm64"],
        "exclude_patterns": ["*.iso"],
        "bwlimit_kbps": 1024,
        "extra_args": ["--verbose"],
    }
    values.update(overrides)
    return Repository(**values)


def _job() -> Job:
    return Job(repo_id=1, tool=Tool.FTPSYNC, attempt=2)


def test_build_ftpsync_plan_maps_repository_configuration(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))
    repository = _repository()
    job = _job()

    plan = build_ftpsync_plan(job, repository, settings)

    assert plan.adapter == Tool.FTPSYNC
    assert plan.source_protocol == "rsync"
    assert plan.slug == "debian"
    assert plan.attempt == 2
    assert plan.architectures == ("amd64", "arm64")
    assert plan.exclude_patterns == ("*.iso",)
    assert plan.bwlimit_kbps == 1024
    assert plan.extra_args == ("--verbose",)
    assert plan.target == settings.data_dir / "debian"
    assert plan.work_dir == settings.tmp_dir / "ftpsync" / "debian"
    assert plan.log_dir == settings.tmp_dir / "ftpsync" / "debian" / "log"


def test_build_ftpsync_plan_rejects_wrong_tool(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))
    repository = _repository(tool=Tool.RSYNC)
    job = _job()

    with pytest.raises(SyncConfigurationError):
        build_ftpsync_plan(job, repository, settings)


def test_build_ftpsync_plan_rejects_wrong_method(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))
    repository = _repository(method=Method.HTTPS)
    job = _job()

    with pytest.raises(SyncConfigurationError):
        build_ftpsync_plan(job, repository, settings)


def test_build_sync_plan_dispatches_ftpsync(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))
    repository = _repository()
    job = _job()

    plan = build_sync_plan(job, repository, settings)

    assert isinstance(plan, FtpsyncPlan)
    assert plan.adapter == Tool.FTPSYNC


def test_build_sync_plan_rejects_unsupported_tool(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))
    repository = _repository(tool=Tool.EXPORT)
    job = _job()

    with pytest.raises(SyncConfigurationError):
        build_sync_plan(job, repository, settings)


def test_build_ftpsync_plan_allows_all_architectures(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))

    plan = build_ftpsync_plan(_job(), _repository(architectures=[]), settings)

    assert plan.architectures == ()
    assert plan.arch_exclude == ()


def test_build_ftpsync_plan_maps_arch_exclude(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))

    plan = build_ftpsync_plan(
        _job(), _repository(architectures=[], arch_exclude=["armel", "armhf"]), settings
    )

    assert plan.architectures == ()
    assert plan.arch_exclude == ("armel", "armhf")


def test_build_ftpsync_plan_reports_one_readable_line(tmp_path: Path) -> None:
    settings = cast(Settings, _Settings(tmp_path))

    with pytest.raises(SyncConfigurationError) as caught:
        build_ftpsync_plan(_job(), _repository(arch_exclude=["armel"]), settings)

    message = str(caught.value)
    assert "\n" not in message
    assert message == "use either ARCH_INCLUDE or ARCH_EXCLUDE, not both"
