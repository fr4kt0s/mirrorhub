"""Concurrent claims must respect repository locks and the global limit."""

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Job, RepoLock, Repository
from mirrorhub.queue.claim import claim_next
from mirrorhub.settings import Settings


def _enqueue(slug: str, count: int) -> None:
    with Session(get_engine()) as db:
        repository = Repository(slug=slug, name=slug)
        db.add(repository)
        db.flush()
        assert repository.id is not None

        for _ in range(count):
            db.add(
                Job(
                    repo_id=repository.id,
                    tool="debmirror",
                    max_attempts=3,
                )
            )
        db.commit()


def _race(workers: int, limit: int, global_limit: int) -> list[int]:
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> int | None:
        barrier.wait(timeout=10)
        claimed = claim_next(
            worker_id=f"concurrent-{index}",
            tool="debmirror",
            limit=limit,
            global_limit=global_limit,
        )
        return claimed.id if claimed else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, range(workers)))

    return [result for result in results if result is not None]


def test_concurrent_claims_allow_only_one_job_per_repo(
    configured: Settings,
) -> None:
    _enqueue("same-repository", 8)
    winners = _race(workers=8, limit=8, global_limit=8)
    assert len(winners) == 1

    with Session(get_engine()) as db:
        assert len(db.exec(select(RepoLock)).all()) == 1


def test_concurrent_claims_respect_global_limit(
    configured: Settings,
) -> None:
    for index in range(8):
        _enqueue(f"repository-{index}", 1)

    winners = _race(workers=8, limit=2, global_limit=2)
    assert len(winners) == 2
    assert len(set(winners)) == 2

    with Session(get_engine()) as db:
        assert len(db.exec(select(RepoLock)).all()) == 2


def test_worker_limit_does_not_reduce_global_parallelism(
    configured: Settings,
) -> None:
    for index in range(4):
        _enqueue(f"repository-{index}", 1)

    winners = _race(workers=4, limit=1, global_limit=4)
    assert len(winners) == 4
    assert len(set(winners)) == 4

    with Session(get_engine()) as db:
        assert len(db.exec(select(RepoLock)).all()) == 4
