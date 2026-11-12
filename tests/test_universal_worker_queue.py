"""Integration tests for a capability-based universal sync worker."""

from sqlmodel import Session, select

from mirrorhub.enums import JobStatus, Tool
from mirrorhub.models import Job, RepoLock, Repository
from mirrorhub.queue import claim
from mirrorhub.services import jobs as jobsvc


def _repo(db: Session, slug: str, tool: Tool) -> Repository:
    repository = Repository(slug=slug, name=slug, tool=tool)
    db.add(repository)
    db.commit()
    db.refresh(repository)
    return repository


def test_capability_worker_claims_multiple_tools(configured) -> None:
    with Session(get_engine()) as db:
        ftpsync_repo = _repo(db, "debian", Tool.FTPSYNC)
        rsync_repo = _repo(db, "ubuntu", Tool.RSYNC)

        ftpsync_job_id = jobsvc.enqueue(db, ftpsync_repo, requested_by="test").id
        rsync_job_id = jobsvc.enqueue(db, rsync_repo, requested_by="test").id

    tools = (Tool.FTPSYNC, Tool.RSYNC)
    first = claim.claim_next(
        worker_id="universal-1",
        tools=tools,
        limit=4,
    )
    second = claim.claim_next(
        worker_id="universal-2",
        tools=tools,
        limit=4,
    )

    assert first is not None
    assert second is not None
    assert {first.tool, second.tool} == {Tool.FTPSYNC.value, Tool.RSYNC.value}
    assert {first.id, second.id} == {ftpsync_job_id, rsync_job_id}

    with Session(get_engine()) as db:
        locks = db.exec(select(RepoLock)).all()
        assert len(locks) == 2
        assert {lock.repo_id for lock in locks} == {
            ftpsync_repo.id,
            rsync_repo.id,
        }


def test_worker_without_capability_does_not_claim_job(configured) -> None:
    with Session(get_engine()) as db:
        repository = _repo(db, "example", Tool.RSYNC)
        jobsvc.enqueue(db, repository, requested_by="test")

    claimed = claim.claim_next(
        worker_id="ftpsync-only",
        tools=(Tool.FTPSYNC,),
        limit=4,
    )

    assert claimed is None

    with Session(get_engine()) as db:
        job = db.exec(select(Job)).first()
        assert job is not None
        assert job.status == JobStatus.QUEUED.value
        assert job.claimed_by is None


def test_worker_concurrency_limits_claims(configured) -> None:
    with Session(get_engine()) as db:
        first_repo = _repo(db, "debian", Tool.FTPSYNC)
        second_repo = _repo(db, "ubuntu", Tool.RSYNC)

        jobsvc.enqueue(db, first_repo, requested_by="test")
        jobsvc.enqueue(db, second_repo, requested_by="test")

    tools = (Tool.FTPSYNC, Tool.RSYNC)
    first = claim.claim_next(
        worker_id="universal-1",
        tools=tools,
        limit=1,
    )
    second = claim.claim_next(
        worker_id="universal-2",
        tools=tools,
        limit=1,
    )

    assert first is not None
    assert second is None

    with Session(get_engine()) as db:
        locks = db.exec(select(RepoLock)).all()
        assert len(locks) == 1


def test_different_repositories_run_in_parallel(configured) -> None:
    with Session(get_engine()) as db:
        for slug in ("debian", "ubuntu", "kali"):
            repository = _repo(db, slug, Tool.FTPSYNC)
            jobsvc.enqueue(db, repository, requested_by="test")

    tools = (Tool.FTPSYNC,)
    first = claim.claim_next(worker_id="universal-1", tools=tools, limit=4)
    second = claim.claim_next(worker_id="universal-2", tools=tools, limit=4)
    third = claim.claim_next(worker_id="universal-3", tools=tools, limit=4)

    assert first is not None
    assert second is not None
    assert third is not None
    assert len({first.repo_id, second.repo_id, third.repo_id}) == 3

    with Session(get_engine()) as db:
        assert len(db.exec(select(RepoLock)).all()) == 3


def test_locked_repository_is_not_claimed_again(configured) -> None:
    with Session(get_engine()) as db:
        repository = _repo(db, "debian", Tool.FTPSYNC)
        jobsvc.enqueue(db, repository, requested_by="test")
        jobsvc.enqueue(db, repository, requested_by="test")

    tools = (Tool.FTPSYNC,)
    first = claim.claim_next(worker_id="universal-1", tools=tools, limit=4)
    second = claim.claim_next(worker_id="universal-2", tools=tools, limit=4)

    assert first is not None
    assert second is None

    with Session(get_engine()) as db:
        locks = db.exec(select(RepoLock)).all()
        assert len(locks) == 1
        assert locks[0].job_id == first.id
