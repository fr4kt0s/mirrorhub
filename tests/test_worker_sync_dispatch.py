import pytest

from mirrorhub.enums import Tool
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Job, Repository
from mirrorhub.worker.main import _build_sync_plan


def test_unsupported_sync_tool_raises(configured) -> None:
    job = Job(repo_id=1, tool=Tool.EXPORT.value, type="sync")
    repository = Repository(
        slug="unsupported",
        name="Unsupported",
        tool=Tool.EXPORT.value,
    )

    with pytest.raises(ExportError, match="no sync adapter registered"):
        _build_sync_plan(job, repository, configured)


def test_non_sync_job_raises(configured) -> None:
    job = Job(repo_id=1, tool=Tool.FTPSYNC.value, type="export")
    repository = Repository(
        slug="debian",
        name="Debian",
        tool=Tool.FTPSYNC.value,
    )

    with pytest.raises(ExportError, match="only sync jobs"):
        _build_sync_plan(job, repository, configured)
