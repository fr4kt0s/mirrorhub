"""Atomic job claiming, per-repo locking, heartbeats, cancellation and stale recovery.

All writes happen inside BEGIN IMMEDIATE transactions, which take SQLite's write lock up
front. "Check the repo lock, then insert it" is therefore atomic without extra machinery,
and a concurrent worker either sees the lock or blocks until the transaction ends.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select

from mirrorhub.db import immediate_session
from mirrorhub.enums import JobStatus, Tool, Trigger
from mirrorhub.models import Job, RepoLock, Repository, utcnow
from mirrorhub.queue import retry as retry_mod

log = logging.getLogger("mirrorhub.queue")

ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    repo_id: int
    tool: str
    attempt: int
    dry_run: bool
    params: dict[str, Any]


def _release_lock(db: Any, job_id: int) -> None:
    lock = db.exec(select(RepoLock).where(RepoLock.job_id == job_id)).first()
    if lock is not None:
        db.delete(lock)


def claim_next(
    *,
    worker_id: str,
    tool: str | None = None,
    tools: Iterable[Tool] | None = None,
    limit: int,
    now: datetime | None = None,
) -> ClaimedJob | None:
    """Claim the oldest eligible job for the supported tools.

    ``tool`` remains supported for single-tool workers. ``tools`` is the
    capability-based interface used by a generic worker that can execute
    several sync methods.
    """
    now = now or utcnow()
    tool_set = frozenset(tools) if tools is not None else None

    if tool_set is not None and not tool_set:
        return None

    with immediate_session() as db:
        running_filter = [col(Job.status) == JobStatus.RUNNING.value]
        if tool_set is not None:
            running_filter.append(col(Job.tool).in_([item.value for item in tool_set]))
        elif tool is not None:
            running_filter.append(col(Job.tool) == tool)

        running = db.exec(select(func.count()).select_from(Job).where(*running_filter)).one()
        if int(running) >= max(limit, 1):
            return None

        candidate_filter = [
            col(Job.status) == JobStatus.QUEUED.value,
            col(Job.next_run_at) <= now,
        ]
        if tool_set is not None:
            candidate_filter.append(col(Job.tool).in_([item.value for item in tool_set]))
        elif tool is not None:
            candidate_filter.append(col(Job.tool) == tool)

        candidates = db.exec(
            select(Job)
            .where(*candidate_filter)
            .order_by(col(Job.next_run_at), col(Job.id))
            .limit(50)
        ).all()

        for job in candidates:
            if job.id is None:
                continue
            if db.exec(select(RepoLock).where(RepoLock.repo_id == job.repo_id)).first() is not None:
                continue
            db.add(
                RepoLock(
                    repo_id=job.repo_id,
                    job_id=job.id,
                    worker_id=worker_id,
                    acquired_at=now,
                    heartbeat_at=now,
                )
            )
            job.status = JobStatus.RUNNING.value
            job.claimed_by = worker_id
            job.started_at = now
            job.heartbeat_at = now
            job.cancel_requested = False
            job.progress_pct = None
            job.progress_text = ""
            db.add(job)
            db.flush()
            return ClaimedJob(
                id=job.id,
                repo_id=job.repo_id,
                tool=job.tool,
                attempt=job.attempt,
                dry_run=job.dry_run,
                params=dict(job.params or {}),
            )
        return None


def heartbeat(
    job_id: int,
    worker_id: str,
    *,
    progress_pct: float | None = None,
    progress_text: str | None = None,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    with immediate_session() as db:
        job = db.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING.value or job.claimed_by != worker_id:
            return False
        job.heartbeat_at = now
        if progress_pct is not None:
            job.progress_pct = max(0.0, min(float(progress_pct), 100.0))
        if progress_text is not None:
            job.progress_text = progress_text[:500]
        db.add(job)
        lock = db.get(RepoLock, job.repo_id)
        if lock is not None and lock.job_id == job_id:
            lock.heartbeat_at = now
            db.add(lock)
        return True


def should_cancel(job_id: int, worker_id: str) -> bool:
    with immediate_session() as db:
        job = db.get(Job, job_id)
        return bool(job and job.claimed_by == worker_id and job.cancel_requested)


def release(job_id: int, worker_id: str) -> None:
    with immediate_session() as db:
        job = db.get(Job, job_id)
        if (
            job is not None
            and job.claimed_by == worker_id
            and job.status != JobStatus.RUNNING.value
        ):
            _release_lock(db, job_id)


def recover_stale(*, stale_after_s: int, now: datetime | None = None) -> list[tuple[int, str]]:
    """Re-queue or fail running jobs whose heartbeat expired, and free their repo locks."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=stale_after_s)
    results: list[tuple[int, str]] = []
    with immediate_session() as db:
        stale = db.exec(
            select(Job).where(
                col(Job.status) == JobStatus.RUNNING.value,
                col(Job.heartbeat_at).is_not(None),
                col(Job.heartbeat_at) < cutoff,
            )
        ).all()
        for job in stale:
            if job.id is None:
                continue
            repo = db.get(Repository, job.repo_id)
            policy = dict(repo.retry_policy or {}) if repo else {}
            _release_lock(db, job.id)
            job.claimed_by = None
            job.heartbeat_at = None
            job.exit_code = None
            if job.attempt < job.max_attempts:
                delay = retry_mod.backoff_seconds(policy, job.attempt)
                job.attempt += 1
                job.status = JobStatus.QUEUED.value
                job.trigger = Trigger.RETRY.value
                job.next_run_at = now + timedelta(seconds=delay)
                job.error_summary = "worker heartbeat expired; requeued"
                results.append((job.id, "requeued"))
            else:
                job.status = JobStatus.FAILED.value
                job.finished_at = now
                job.error_summary = "worker heartbeat expired; attempts exhausted"
                results.append((job.id, "failed"))
            job.started_at = None
            db.add(job)
        return results
