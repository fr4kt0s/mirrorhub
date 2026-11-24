import threading
from pathlib import Path

import pytest
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.enums import Tool
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Job, RepoLock, Repository, default_retry_policy
from mirrorhub.queue import claim
from mirrorhub.services import jobs as jobsvc
from mirrorhub.settings import Settings
from mirrorhub.worker.main import _build_sync_plan, _execute


def test_unsupported_sync_tool_raises(configured: Settings) -> None:
    job = Job(id=1, repo_id=1, tool=Tool.EXPORT.value, type="sync")
    repository = Repository(
        id=1,
        slug="unsupported",
        name="Unsupported",
        tool=Tool.EXPORT.value,
    )

    with pytest.raises(ExportError, match="no sync adapter registered"):
        _build_sync_plan(job, repository, configured)


def test_non_sync_job_raises(configured: Settings) -> None:
    job = Job(id=1, repo_id=1, tool=Tool.FTPSYNC.value, type="export")
    repository = Repository(
        id=1,
        slug="debian",
        name="Debian",
        tool=Tool.FTPSYNC.value,
    )

    with pytest.raises(ExportError, match="only sync jobs"):
        _build_sync_plan(job, repository, configured)


def test_configuration_error_fails_without_retry(configured: Settings) -> None:
    """A repository that cannot produce a plan fails once, with the reason, not a retry."""
    with Session(get_engine()) as db:
        repository = Repository(
            slug="debian-ftpsync",
            name="Debian FTPsync",
            kind="apt",
            tool=Tool.FTPSYNC.value,
            method="rsync",
            upstream_host="deb.debian.org",
            upstream_root="/debian",
            # Not reachable through the form; stored directly to break the plan.
            architectures=["amd64"],
            arch_exclude=["armel"],
            retry_policy=default_retry_policy(),
        )
        db.add(repository)
        db.commit()
        db.refresh(repository)
        job_id = jobsvc.enqueue(db, repository).id
        repo_id = repository.id

    claimed = claim.claim_next(worker_id="config-test", tool=Tool.FTPSYNC.value, limit=4)
    assert claimed is not None
    assert claimed.id == job_id

    _execute(claimed, "config-test", configured, threading.Event())

    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.attempt == 1
        assert job.exit_code is None
        assert job.error_summary == (
            "configuration error: use either ARCH_INCLUDE or ARCH_EXCLUDE, not both"
        )
        assert db.get(RepoLock, repo_id) is None
        assert job.log_path
    log_text = Path(job.log_path).read_text(encoding="utf-8")
    assert "# configuration error: use either ARCH_INCLUDE or ARCH_EXCLUDE" in log_text
    assert "execution failed" not in log_text
