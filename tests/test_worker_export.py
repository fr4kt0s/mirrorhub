"""Worker integration and transactional repository-lock release."""

import threading
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.models import Job, RepoLock, Repository
from mirrorhub.queue import claim
from mirrorhub.services import export_jobs
from mirrorhub.settings import Settings
from mirrorhub.worker.main import _execute, finish


def _repo(settings: Settings) -> int:
    with Session(get_engine()) as db:
        repo = Repository(
            slug="files",
            name="Files",
            kind="generic",
            tool="wget",
            export_format="tar",
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        repo_id = repo.id or 0

    directory = settings.data_dir / "files"
    directory.mkdir()
    (directory / "file.txt").write_bytes(b"example")
    return repo_id


def test_export_worker_publishes_and_releases_lock(configured: Settings) -> None:
    repo_id = _repo(configured)
    job_id = export_jobs.enqueue(repo_id, mode="full", actor="test")
    claimed = claim.claim_next(worker_id="export-test", tool="export", limit=4)
    assert claimed is not None

    _execute(claimed, "export-test", configured, threading.Event())

    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert db.get(RepoLock, repo_id) is None
    assert len(list(configured.export_dir.rglob(".done"))) == 1


def test_retry_releases_repository_lock(configured: Settings) -> None:
    repo_id = _repo(configured)
    job_id = export_jobs.enqueue(repo_id, mode="full", actor="test")
    claimed = claim.claim_next(worker_id="retry-test", tool="export", limit=4)
    assert claimed is not None

    assert finish(
        job_id,
        "retry-test",
        status="queued",
        exit_code=1,
        retry_delay=60,
    )
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.attempt == 2
        assert job.claimed_by is None
        assert db.get(RepoLock, repo_id) is None


def test_foreign_worker_cannot_finalize(configured: Settings) -> None:
    repo_id = _repo(configured)
    job_id = export_jobs.enqueue(repo_id, mode="full", actor="test")
    assert claim.claim_next(worker_id="owner", tool="export", limit=4) is not None
    assert not finish(job_id, "other", status="failed", exit_code=1)

    with Session(get_engine()) as db:
        assert db.get(RepoLock, repo_id) is not None


def test_recover_published_export(configured: Settings) -> None:
    repo_id = _repo(configured)
    job_id = export_jobs.enqueue(repo_id, mode="full", actor="test")
    claimed = claim.claim_next(worker_id="export-test", tool="export", limit=4)
    assert claimed is not None
    _execute(claimed, "export-test", configured, threading.Event())

    identifier = export_jobs.published_for_job(job_id, configured)
    assert identifier is not None
    assert (configured.export_dir / "files" / identifier / ".done").is_file()


def test_exports_page_requires_login(client: TestClient) -> None:
    response = client.get("/exports")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_export_post_requires_csrf(admin: TestClient) -> None:
    response = admin.post("/exports/1/start", data={"mode": "full"})
    assert response.status_code == 403


def test_export_page_is_available(admin: TestClient) -> None:
    response = admin.get("/exports")
    assert response.status_code == 200
    assert "Export history" in response.text


def test_verify_module_has_no_application_imports() -> None:
    import ast
    import importlib.util

    spec = importlib.util.find_spec("mirrorhub.verify")
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("mirrorhub")
        elif isinstance(node, ast.Import):
            assert all(not alias.name.startswith("mirrorhub") for alias in node.names)
