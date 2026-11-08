"""Job records: enqueue, cancel requests, listings. Execution lives in the worker."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from mirrorhub.enums import JobStatus, JobType, Tool, Trigger
from mirrorhub.models import Job, Repository, utcnow
from mirrorhub.services import audit
from sqlalchemy import delete, update

from mirrorhub.models import Export, Job, ManifestSnapshot, RepoLock, Repository, utcnow
from collections.abc import Collection

ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.CANCELLING.value)

# Phase 6 flips this on once the export worker can execute export jobs.
EXPORT_CHAIN_ENABLED = False


def chain_export_enabled() -> bool:
    return EXPORT_CHAIN_ENABLED


def enqueue(
    db: Session,
    repo: Repository,
    *,
    job_type: str = JobType.SYNC.value,
    trigger: str = Trigger.MANUAL.value,
    dry_run: bool = False,
    params: dict[str, Any] | None = None,
    requested_by: str = "system",
    parent_job_id: int | None = None,
) -> Job:
    tool = repo.tool if job_type == JobType.SYNC.value else Tool.EXPORT.value
    policy = dict(repo.retry_policy or {})
    job = Job(
        repo_id=repo.id,
        type=job_type,
        tool=tool,
        trigger=trigger,
        dry_run=dry_run,
        params=params or {},
        status=JobStatus.QUEUED.value,
        attempt=1,
        max_attempts=int(policy.get("max_attempts", 1)),
        next_run_at=utcnow(),
        parent_job_id=parent_job_id,
        requested_by=requested_by,
    )
    db.add(job)
    db.flush()
    audit.record(
        db,
        actor=requested_by,
        action="job.enqueue",
        target_type="job",
        target_id=str(job.id),
        detail={
            "repo": repo.slug,
            "job_type": job_type,
            "tool": tool,
            "dry_run": dry_run,
            "trigger": trigger,
        },
        commit=False,
    )
    db.commit()
    db.refresh(job)
    return job


def find_active(db: Session, repo_id: int, job_type: str) -> Job | None:
    return db.exec(
        select(Job)
        .where(
            Job.repo_id == repo_id,
            Job.type == job_type,
            col(Job.status).in_(list(ACTIVE)),
        )
        .order_by(col(Job.id).desc())
    ).first()


def get(db: Session, job_id: int) -> Job | None:
    return db.get(Job, job_id)


def recent(db: Session, limit: int = 100) -> list[Job]:
    return list(db.exec(select(Job).order_by(col(Job.id).desc()).limit(limit)).all())


def for_repo(db: Session, repo_id: int, limit: int = 50) -> list[Job]:
    return list(
        db.exec(
            select(Job).where(Job.repo_id == repo_id).order_by(col(Job.id).desc()).limit(limit)
        ).all()
    )


def request_cancel(db: Session, job: Job, *, actor: str) -> str:
    """Cancel a queued job outright; flag a running one so its worker stops it."""
    if job.status == JobStatus.QUEUED.value:
        job.status = JobStatus.CANCELLED.value
        job.finished_at = utcnow()
        job.error_summary = "cancelled before start"
        action = "job.cancel"
    elif job.status == JobStatus.RUNNING.value:
        job.cancel_requested = True
        action = "job.cancel_requested"
    else:
        return "ignored"
    db.add(job)
    audit.record(
        db,
        actor=actor,
        action=action,
        target_type="job",
        target_id=str(job.id),
        detail={"status": job.status},
        commit=False,
    )
    db.commit()
    return action


def _release_references(db: Session, ids: list[int]) -> None:
    """Drop or clear every row that points at the given jobs."""
    if not ids:
        return
    db.exec(update(Job).where(col(Job.parent_job_id).in_(ids)).values(parent_job_id=None))
    db.exec(update(Export).where(col(Export.job_id).in_(ids)).values(job_id=None))
    db.exec(
        update(ManifestSnapshot).where(col(ManifestSnapshot.job_id).in_(ids)).values(job_id=None)
    )
    db.exec(delete(RepoLock).where(col(RepoLock.job_id).in_(ids)))
    db.flush()


def delete_job(db: Session, job: Job, *, actor: str, finished: Collection[str]) -> bool:
    """Delete one finished job. Active jobs are refused."""
    if job.status not in finished:
        return False
    job_id, repo_id, status = job.id, job.repo_id, job.status
    _release_references(db, [job_id])
    db.delete(job)
    audit.record(
        db,
        actor=actor,
        action="job.delete",
        target_type="job",
        target_id=str(job_id),
        detail={"repo_id": repo_id, "status": status},
        commit=False,
    )
    db.commit()
    return True


def delete_finished(
    db: Session,
    *,
    actor: str,
    finished: Collection[str],
    status: str = "",
    repo_id: int = 0,
) -> int:
    """Delete finished jobs, optionally limited by status or repository."""
    statement = select(Job).where(col(Job.status).in_(list(finished)))
    if status:
        statement = statement.where(col(Job.status) == status)
    if repo_id:
        statement = statement.where(Job.repo_id == repo_id)
    rows = list(db.exec(statement).all())
    if not rows:
        return 0

    ids = [row.id for row in rows if row.id is not None]
    _release_references(db, ids)
    for row in rows:
        db.delete(row)
    audit.record(
        db,
        actor=actor,
        action="jobs.cleanup",
        target_type="job",
        target_id="*",
        detail={"count": len(ids), "status": status or "all finished", "repo_id": repo_id},
        commit=False,
    )
    db.commit()
    return len(ids)
