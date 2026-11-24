"""Automatic job retention, hourly scheduling and database VACUUM."""

from datetime import timedelta

from sqlmodel import Session, col, select

from mirrorhub import db as dbmod
from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog, Job, Repository, Setting, utcnow
from mirrorhub.services import jobs, logs, scheduler
from mirrorhub.settings import Settings


def _job(
    db: Session,
    repo_id: int,
    *,
    days_ago: float,
    status: str = "succeeded",
    job_type: str = "sync",
    notified: bool = True,
) -> int:
    finished = utcnow() - timedelta(days=days_ago)
    job = Job(
        repo_id=repo_id,
        tool="debmirror",
        type=job_type,
        status=status,
        finished_at=finished,
        notified_at=finished if notified else None,
    )
    db.add(job)
    db.commit()
    assert job.id is not None
    return job.id


def test_purge_old_keeps_newest_recent_and_unnotified(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="r", name="r")
        db.add(repo)
        db.commit()
        assert repo.id is not None
        old = _job(db, repo.id, days_ago=90)
        old_cancelled = _job(db, repo.id, days_ago=80, status="cancelled", notified=False)
        old_unnotified = _job(db, repo.id, days_ago=70, status="failed", notified=False)
        newest_export = _job(db, repo.id, days_ago=60, job_type="export")
        recent = _job(db, repo.id, days_ago=1)
        running = Job(repo_id=repo.id, tool="debmirror", status="running")
        db.add(running)
        db.commit()
        path = logs.job_log_path(old, configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n")

        assert jobs.purge_old(db, 30) == 2

        left = {row.id for row in db.exec(select(Job)).all()}
        assert old not in left and old_cancelled not in left
        assert {old_unnotified, newest_export, recent, running.id} <= left
        assert not path.exists()
        audit = db.exec(select(AuditLog).where(AuditLog.action == "jobs.retention")).one()
        assert audit.actor == "scheduler"
        assert jobs.purge_old(db, 30) == 0
        assert jobs.purge_old(db, 0) == 0


def test_newest_finished_job_survives_even_when_old(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="rare", name="rare")
        db.add(repo)
        db.commit()
        assert repo.id is not None
        only = _job(db, repo.id, days_ago=400)
        assert jobs.purge_old(db, 30) == 0
        assert db.get(Job, only) is not None


def test_retention_runs_at_most_hourly() -> None:
    scheduler._last_retention = None
    now = [1000.0]
    assert scheduler._retention_due(lambda: now[0])
    now[0] += 3599
    assert not scheduler._retention_due(lambda: now[0])
    now[0] += 2
    assert scheduler._retention_due(lambda: now[0])
    scheduler._last_retention = None


def test_vacuum_shrinks_the_file(configured: Settings) -> None:
    with Session(get_engine()) as db:
        for index in range(200):
            db.add(Setting(key=f"filler-{index}", value="x" * 20000))
        db.commit()
        for row in db.exec(select(Setting).where(col(Setting.key).startswith("filler-"))).all():
            db.delete(row)
        db.commit()
    get_engine().dispose()

    before, after = dbmod.vacuum()
    assert after < before
    assert dbmod._main(["vacuum"]) == 0
    assert dbmod._main(["nonsense"]) == 2
