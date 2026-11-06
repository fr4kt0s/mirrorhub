"""Cron helpers and schedule firing against a real SQLite database."""

from datetime import timedelta

from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Job, Repository, Schedule, utcnow
from mirrorhub.services import scheduler


def _repo(db: Session, slug: str) -> int:
    repo = Repository(slug=slug, name=slug)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo.id or 0


def test_next_after_is_strictly_in_the_future() -> None:
    base = utcnow()
    nxt = scheduler.next_after("0 3 * * *", base)
    assert nxt > base
    assert nxt.hour == 3 and nxt.minute == 0


def test_presets_are_valid_cron_expressions() -> None:
    from mirrorhub.security import validators

    for key, (expr, _label) in scheduler.PRESETS.items():
        assert validators.validate_cron(expr) is None, key


def test_ensure_next_fire_fills_missing_values(configured) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        db.add(Schedule(repo_id=repo_id, cron_expr="0 3 * * *", enabled=True))
        db.commit()
    with Session(get_engine()) as db:
        assert scheduler.ensure_next_fire(db) == 1
        row = db.exec(select(Schedule)).one()
        assert row.next_fire_at is not None


def test_fire_due_creates_one_job_and_advances(configured) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        schedule = Schedule(repo_id=repo_id, cron_expr="0 3 * * *", enabled=True)
        db.add(schedule)
        db.commit()
        db.refresh(schedule)
        schedule.next_fire_at = utcnow() - timedelta(minutes=1)
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id or 0

    with Session(get_engine()) as db:
        created = scheduler.fire_due(db)
    assert len(created) == 1

    with Session(get_engine()) as db:
        jobs = db.exec(select(Job)).all()
        assert len(jobs) == 1
        assert jobs[0].trigger == "schedule"
        assert jobs[0].params.get("schedule_id") == schedule_id
        row = db.get(Schedule, schedule_id)
        assert row is not None
        assert row.last_fire_at is not None
        assert row.next_fire_at > utcnow()


def test_fire_due_skips_when_a_job_is_active(configured) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        schedule = Schedule(
            repo_id=repo_id,
            cron_expr="0 3 * * *",
            enabled=True,
            next_fire_at=utcnow() - timedelta(minutes=1),
        )
        db.add(schedule)
        db.commit()
        db.add(Job(repo_id=repo_id, tool="debmirror", max_attempts=3))
        db.commit()

    with Session(get_engine()) as db:
        assert scheduler.fire_due(db) == []
    with Session(get_engine()) as db:
        assert len(db.exec(select(Job)).all()) == 1


def test_disabled_schedule_never_fires(configured) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db, "debian")
        db.add(
            Schedule(
                repo_id=repo_id,
                cron_expr="0 3 * * *",
                enabled=False,
                next_fire_at=utcnow() - timedelta(hours=1),
            )
        )
        db.commit()
    with Session(get_engine()) as db:
        assert scheduler.fire_due(db) == []
