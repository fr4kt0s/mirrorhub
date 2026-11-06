"""Queue claiming: per-repo locking, parallel repos, global concurrency limit.

Model objects only live inside their session block here: scalar ids are captured before
the block closes, because expire_on_commit=True would make later attribute reads fail.
"""

from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Job, RepoLock, Repository
from mirrorhub.queue import claim
from mirrorhub.services import jobs as jobsvc


def _repo(db: Session, slug: str) -> Repository:
    repo = Repository(slug=slug, name=slug)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def test_claim_locks_the_repository(configured) -> None:
    with Session(get_engine()) as db:
        repo = _repo(db, "debian")
        repo_id = repo.id or 0
        first_id = jobsvc.enqueue(db, repo, requested_by="test").id or 0
        jobsvc.enqueue(db, repo, requested_by="test")

    claimed = claim.claim_next(worker_id="w1", tool="debmirror", limit=4)
    assert claimed is not None
    assert claimed.id == first_id
    assert claim.claim_next(worker_id="w2", tool="debmirror", limit=4) is None

    with Session(get_engine()) as db:
        job = db.get(Job, first_id)
        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert job.claimed_by == "w1"
        lock = db.get(RepoLock, repo_id)
        assert lock is not None
        assert lock.job_id == first_id


def test_different_repositories_run_in_parallel(configured) -> None:
    with Session(get_engine()) as db:
        for slug in ("debian", "ubuntu", "kali"):
            jobsvc.enqueue(db, _repo(db, slug), requested_by="test")

    first = claim.claim_next(worker_id="w1", tool="debmirror", limit=4)
    second = claim.claim_next(worker_id="w2", tool="debmirror", limit=4)
    assert first is not None and second is not None
    assert first.repo_id != second.repo_id
    with Session(get_engine()) as db:
        assert len(db.exec(select(RepoLock)).all()) == 2


def test_global_concurrency_limit(configured) -> None:
    with Session(get_engine()) as db:
        for slug in ("debian", "ubuntu"):
            jobsvc.enqueue(db, _repo(db, slug), requested_by="test")
    assert claim.claim_next(worker_id="w1", tool="debmirror", limit=1) is not None
