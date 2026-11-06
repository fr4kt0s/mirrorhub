"""Stale-job recovery: requeue with backoff, or fail when attempts are exhausted."""

from datetime import timedelta

from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus, Trigger
from mirrorhub.models import Job, RepoLock, Repository, utcnow
from mirrorhub.queue import claim
from mirrorhub.services import jobs as jobsvc


def _claim_one(slug: str = "debian", max_attempts: int = 3) -> tuple[int, int]:
    with Session(get_engine()) as db:
        repo = Repository(slug=slug, name=slug)
        db.add(repo)
        db.commit()
        db.refresh(repo)
        job = jobsvc.enqueue(db, repo, requested_by="test")
        job.max_attempts = max_attempts
        db.add(job)
        db.commit()
        repo_id = repo.id or 0
        job_id = job.id or 0
    claimed = claim.claim_next(worker_id="w1", tool="debmirror", limit=4)
    assert claimed is not None and claimed.id == job_id
    return repo_id, job_id


def _age_heartbeat(job_id: int, minutes: int = 10) -> None:
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.heartbeat_at = utcnow() - timedelta(minutes=minutes)
        db.add(job)
        db.commit()


def test_stale_job_is_requeued(configured) -> None:
    repo_id, job_id = _claim_one(max_attempts=3)
    _age_heartbeat(job_id)

    results = claim.recover_stale(stale_after_s=60)
    assert results == [(job_id, "requeued")]

    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value
        assert job.attempt == 2
        assert job.claimed_by is None
        assert job.trigger == Trigger.RETRY.value
        assert job.next_run_at > utcnow()
        assert db.get(RepoLock, repo_id) is None


def test_stale_job_fails_when_attempts_are_exhausted(configured) -> None:
    repo_id, job_id = _claim_one(max_attempts=1)
    _age_heartbeat(job_id)

    results = claim.recover_stale(stale_after_s=60)
    assert results == [(job_id, "failed")]

    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED.value
        assert job.finished_at is not None
        assert "attempts exhausted" in job.error_summary
        assert db.get(RepoLock, repo_id) is None


def test_fresh_heartbeat_is_left_alone(configured) -> None:
    _repo_id, job_id = _claim_one()
    assert claim.recover_stale(stale_after_s=600) == []
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.status == JobStatus.RUNNING.value
