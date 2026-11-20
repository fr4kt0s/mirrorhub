"""Job ids are never reused, and job logs never leak into a later job."""

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Export, Job, RepoLock, Repository
from mirrorhub.services import jobs as jobsvc
from mirrorhub.services import logs
from mirrorhub.settings import Settings

FINISHED = {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("prepend_sys_path", str(root / "src"))
    return config


def _repo(db: Session) -> int:
    repo = Repository(slug="mint", name="Mint", tool="ftpsync")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    assert repo.id is not None
    return repo.id


def _job_table_sql() -> str:
    with get_engine().connect() as connection:
        return str(
            connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job'")
            ).scalar_one()
        )


def test_ids_are_not_reused_after_deleting_the_newest_jobs(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        for _ in range(3):
            db.add(Job(repo_id=repo_id, status=JobStatus.SUCCEEDED.value))
        db.commit()

        assert jobsvc.delete_finished(db, actor="tester", finished=FINISHED) == 3

        new = Job(repo_id=repo_id)
        db.add(new)
        db.commit()
        db.refresh(new)
        assert new.id == 4


def test_deleting_jobs_removes_their_logs(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo_id = _repo(db)
        done = [Job(repo_id=repo_id, status=JobStatus.SUCCEEDED.value) for _ in range(2)]
        active = Job(repo_id=repo_id, status=JobStatus.QUEUED.value)
        single = Job(repo_id=repo_id, status=JobStatus.FAILED.value)
        for job in (*done, active, single):
            db.add(job)
        db.commit()
        ids = {}
        for name, job in (("a", done[0]), ("b", done[1]), ("active", active), ("one", single)):
            db.refresh(job)
            assert job.id is not None
            ids[name] = job.id
            logs.job_log_path(job.id, configured).write_text("output\n", encoding="utf-8")

        assert jobsvc.delete_job(db, single, actor="tester", finished=FINISHED)
        assert not logs.job_log_path(ids["one"], configured).exists()

        assert jobsvc.delete_finished(db, actor="tester", finished={JobStatus.SUCCEEDED.value}) == 2
        assert not logs.job_log_path(ids["a"], configured).exists()
        assert not logs.job_log_path(ids["b"], configured).exists()
        assert logs.job_log_path(ids["active"], configured).exists()


def test_remove_job_logs_tolerates_missing_files(configured: Settings) -> None:
    assert logs.remove_job_logs([12345], configured) == 0


def test_quarantine_moves_stale_log_aside(tmp_path: Path) -> None:
    path = tmp_path / "18.log"
    assert logs.quarantine_stale(path) is None

    path.write_text("$ debmirror ...\n", encoding="utf-8")
    moved = logs.quarantine_stale(path)

    assert moved is not None
    assert not path.exists()
    assert moved.name.startswith("18.stale-") and moved.suffix == ".log"
    assert moved.read_text(encoding="utf-8") == "$ debmirror ...\n"


def test_run_header_uses_utc_for_naive_timestamps() -> None:
    header = logs.run_header(
        18, 2, "packages.linuxmint.com", "sync", "ftpsync", "w1", datetime(2026, 11, 20, 22, 11, 38)
    )
    assert header == (
        "# run job 18 attempt 2 repo packages.linuxmint.com type sync tool ftpsync "
        "worker w1 started 2026-11-20T22:11:38Z\n"
    )


def test_tail_current_run_starts_at_last_header(tmp_path: Path) -> None:
    path = tmp_path / "18.log"
    started = datetime(2026, 11, 20, 22, 0, 0)
    path.write_text(
        "legacy line without header\n"
        + logs.run_header(18, 1, "mint", "sync", "ftpsync", "w1", started)
        + "attempt 1 output\n"
        + logs.run_header(18, 2, "mint", "sync", "ftpsync", "w1", started)
        + "attempt 2 output\n",
        encoding="utf-8",
    )

    lines = logs.tail_current_run(path, 50)

    assert lines[0].startswith("# run job 18 attempt 2 ")
    assert lines[1:] == ["attempt 2 output"]


def test_tail_current_run_without_header_equals_tail(tmp_path: Path) -> None:
    path = tmp_path / "old.log"
    path.write_text("one\ntwo\n", encoding="utf-8")
    assert logs.tail_current_run(path, 50) == logs.tail(path, 50)


def test_0006_rebuilds_job_with_autoincrement_and_keeps_references(
    configured: Settings,
) -> None:
    config = _alembic_config()
    command.upgrade(config, "head")

    # Recreate the pre-0006 job table (same DDL, without AUTOINCREMENT).
    with get_engine().begin() as connection:
        statements = (
            connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE tbl_name = 'job' AND sql IS NOT NULL "
                    "ORDER BY type DESC"
                )
            )
            .scalars()
            .all()
        )
        connection.execute(text("DROP TABLE job"))
        for statement in statements:
            connection.execute(text(statement.replace(" AUTOINCREMENT", "")))
        connection.execute(text("DELETE FROM sqlite_sequence WHERE name = 'job'"))
    assert "AUTOINCREMENT" not in _job_table_sql().upper()

    with Session(get_engine()) as db:
        repo_id = _repo(db)
        db.add(Job(id=1, repo_id=repo_id, status=JobStatus.SUCCEEDED.value))
        db.add(Job(id=2, repo_id=repo_id, status=JobStatus.SUCCEEDED.value))
        db.commit()
        db.add(Job(id=3, repo_id=repo_id, status=JobStatus.RUNNING.value, parent_job_id=2))
        db.commit()
        db.add(Export(export_id="20261120T000000Z-full", repo_id=repo_id, job_id=2))
        db.add(RepoLock(repo_id=repo_id, job_id=3, worker_id="w1"))
        db.commit()
    with get_engine().begin() as connection:
        # Job 7 existed once and was deleted; only the append-only audit log remembers it.
        connection.execute(
            text(
                "INSERT INTO audit_log (ts, actor, ip, action, target_type, target_id, detail) "
                "VALUES ('2026-11-20 00:00:00', 'worker', '', 'job.start', 'job', '7', '{}'), "
                "('2026-11-20 00:00:00', 'admin', '', 'jobs.cleanup', 'job', '*', '{}')"
            )
        )
        connection.execute(text("INSERT INTO setting (key, value) VALUES ('notified_job_id', '5')"))

    command.stamp(config, "0005")
    command.upgrade(config, "head")

    assert "AUTOINCREMENT" in _job_table_sql().upper()
    with Session(get_engine()) as db:
        assert [job.id for job in db.exec(select(Job).order_by(col(Job.id))).all()] == [1, 2, 3]
        child = db.get(Job, 3)
        assert child is not None
        assert child.parent_job_id == 2
        assert db.exec(select(Export)).one().job_id == 2
        assert db.exec(select(RepoLock)).one().job_id == 3

        new = Job(repo_id=repo_id)
        db.add(new)
        db.commit()
        db.refresh(new)
        assert new.id == 8

    with get_engine().connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        indexes = set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'job'")
            )
            .scalars()
            .all()
        )
    assert {"ix_job_claim", "ix_job_repo_status"} <= indexes

    command.downgrade(config, "0005")
    assert "AUTOINCREMENT" not in _job_table_sql().upper()
    with Session(get_engine()) as db:
        assert len(db.exec(select(Job)).all()) == 4
