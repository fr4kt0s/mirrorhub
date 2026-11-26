"""Rehash job: re-read every file, rebuild the hash cache, report silent changes."""

import threading
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.export import manifest, rehash
from mirrorhub.export.job import hash_cache_path
from mirrorhub.models import Job, RepoLock, Repository, Schedule
from mirrorhub.queue import claim
from mirrorhub.services import jobs as jobsvc
from mirrorhub.services import scheduler, yamlio
from mirrorhub.settings import Settings
from mirrorhub.worker.main import _execute


def _pulse(_pct: float | None, _text: str) -> None:
    pass


def _tree(root: Path) -> None:
    root.mkdir()
    (root / "a.txt").write_bytes(b"alpha")
    (root / "sub").mkdir()
    (root / "sub" / "b.bin").write_bytes(b"b" * 100_000)
    (root / "link").symlink_to("a.txt")


def test_rehash_rebuilds_the_cache(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _tree(root)
    cache_path = tmp_path / "cache.json"
    cache: manifest.HashCache = {}
    manifest.snapshot(root, cache=cache, racy_margin_ns=0)
    cache["gone.txt"] = [1, 2, 3, 4, "0" * 64]
    manifest.save_hash_cache(cache_path, cache)

    result = rehash.run(root, cache_path, _pulse, threads=4, racy_margin_ns=0)

    assert result.files == 2 and result.compared == 2 and result.mismatched == []
    assert result.bytes == 100_005
    rebuilt = manifest.load_hash_cache(cache_path)
    assert set(rebuilt) == {"a.txt", "sub/b.bin"}
    inventory = manifest.snapshot(root)
    assert rebuilt["sub/b.bin"][4] == inventory["sub/b.bin"]["sha256"]


def test_rehash_reports_content_changed_without_stat_change(tmp_path: Path) -> None:
    """A cache entry whose stat key still matches but whose digest is wrong stands for
    content that changed underneath (ctime cannot be reset from user space, so the
    test falsifies the cached digest instead). The old entry is kept."""
    root = tmp_path / "repo"
    _tree(root)
    cache_path = tmp_path / "cache.json"
    cache: manifest.HashCache = {}
    manifest.snapshot(root, cache=cache, racy_margin_ns=0)
    cache["a.txt"][4] = "f" * 64
    manifest.save_hash_cache(cache_path, cache)

    result = rehash.run(root, cache_path, _pulse, racy_margin_ns=0)

    assert result.mismatched == ["a.txt"]
    assert "1 changed content without a stat change" in result.summary()
    assert manifest.load_hash_cache(cache_path)["a.txt"][4] == "f" * 64


def test_rehash_skips_racy_files_in_the_cache(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _tree(root)
    cache_path = tmp_path / "cache.json"
    result = rehash.run(root, cache_path, _pulse)  # files are fresh: racy
    assert result.files == 2 and result.compared == 0
    assert manifest.load_hash_cache(cache_path) == {}


def _repository(settings: Settings) -> Repository:
    with Session(get_engine()) as db:
        repo = Repository(slug="files", name="Files", kind="generic", tool="wget")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        detached = Repository(**repo.model_dump())
    _tree(settings.data_dir / "files")
    return detached


def _run_rehash(repo: Repository, settings: Settings) -> Job:
    with Session(get_engine()) as db:
        job = jobsvc.enqueue(db, repo, job_type="rehash")
        assert job.tool == "export"
        job_id = job.id
    claimed = claim.claim_next(worker_id="rehash-test", tool="export", limit=4)
    assert claimed is not None and claimed.id == job_id
    _execute(claimed, "rehash-test", settings, threading.Event())
    with Session(get_engine()) as db:
        done = db.get(Job, job_id)
        assert done is not None
        assert db.get(RepoLock, repo.id) is None
        return Job(**done.model_dump())


def test_rehash_job_succeeds(configured: Settings) -> None:
    repo = _repository(configured)
    job = _run_rehash(repo, configured)
    assert job.status == "succeeded", job.error_summary
    log = Path(job.log_path or "").read_text()
    assert "rehashed 2 files" in log


def test_rehash_job_fails_on_silent_change(configured: Settings) -> None:
    repo = _repository(configured)
    root = configured.data_dir / "files"
    cache: manifest.HashCache = {}
    manifest.snapshot(root, cache=cache, racy_margin_ns=0)
    cache["sub/b.bin"][4] = "0" * 64
    manifest.save_hash_cache(hash_cache_path(configured, "files"), cache)

    job = _run_rehash(repo, configured)

    assert job.status == "failed"
    assert job.attempt == 1  # no retry
    assert "1 files changed content without a stat change" in (job.error_summary or "")
    assert (
        "content changed without a stat change: sub/b.bin" in Path(job.log_path or "").read_text()
    )


def test_schedule_fires_a_rehash_job(configured: Settings) -> None:
    repo = _repository(configured)
    with Session(get_engine()) as db:
        db.add(Schedule(repo_id=repo.id or 0, job_type="rehash", cron_expr="0 4 1 * *"))
        db.commit()
        scheduler.ensure_next_fire(db)
        schedule = db.exec(select(Schedule)).one()
        created = scheduler.fire_due(db, now=schedule.next_fire_at)
        assert len(created) == 1
        job = db.get(Job, created[0])
        assert job is not None and job.type == "rehash" and job.tool == "export"


def test_schedule_form_accepts_rehash(admin: TestClient, csrf_token: str) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="files", name="Files", kind="generic", tool="wget")
        db.add(repo)
        db.commit()
        repo_id = repo.id
    response = admin.post(
        "/schedules",
        data={
            "csrf_token": csrf_token,
            "repo_id": str(repo_id),
            "job_type": "rehash",
            "cron_expr": "0 4 1 * *",
            "enabled": "on",
            "chain_export": "always",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(get_engine()) as db:
        schedule = db.exec(select(Schedule)).one()
        assert schedule.job_type == "rehash"
        assert schedule.chain_export is None
    page = admin.get("/schedules").text
    assert "Rehash" in page


def test_schedule_form_rejects_unknown_job_type(admin: TestClient, csrf_token: str) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="files", name="Files", kind="generic", tool="wget")
        db.add(repo)
        db.commit()
        repo_id = repo.id
    response = admin.post(
        "/schedules",
        data={
            "csrf_token": csrf_token,
            "repo_id": str(repo_id),
            "job_type": "nonsense",
            "cron_expr": "0 4 1 * *",
        },
    )
    assert "job type must be sync, export or rehash" in response.text


def test_manual_rehash_button(admin: TestClient, csrf_token: str) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="files", name="Files", kind="generic", tool="wget")
        db.add(repo)
        db.commit()
        repo_id = repo.id
    assert "/rehash" in admin.get("/jobs").text
    response = admin.post(
        f"/jobs/{repo_id}/rehash",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(get_engine()) as db:
        job = db.exec(select(Job)).one()
        assert job.type == "rehash"


def test_yaml_schedule_job_type_is_validated(configured: Settings) -> None:
    document = {
        "schedules": [
            {"repo_slug": "files", "cron_expr": "0 4 * * *", "job_type": "rehash"},
            {"repo_slug": "files", "cron_expr": "0 4 * * *", "job_type": "bogus"},
        ]
    }
    with Session(get_engine()) as db:
        plan = yamlio.build_plan(db, document)
    assert [entry["job_type"] for entry in plan.schedules] == ["rehash"]
    assert any("job_type must be one of" in error for error in plan.errors)
