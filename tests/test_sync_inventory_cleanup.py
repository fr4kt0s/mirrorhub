"""Sync inventories: written only for chained exports, deleted after use, swept when stale."""

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.export import job as export_engine
from mirrorhub.models import Job, Repository
from mirrorhub.queue import claim
from mirrorhub.services import export_jobs
from mirrorhub.settings import Settings
from mirrorhub.sync.result import SyncResult, SyncStatus
from mirrorhub.worker import main as worker_main
from mirrorhub.worker.runner import RunResult

DAY = 86400


def _pulse(_pct: float | None, _text: str) -> None:
    pass


def _repo(settings: Settings, *, chain_export: bool = False) -> Repository:
    with Session(get_engine()) as db:
        repo = Repository(
            slug="files",
            name="Files",
            kind="generic",
            tool="wget",
            export_format="tar",
            chain_export=chain_export,
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        detached = Repository(**repo.model_dump())
    directory = settings.data_dir / "files"
    directory.mkdir()
    (directory / "file.txt").write_bytes(b"example")
    return detached


def _inventory(settings: Settings, name: str, age_s: float = 0) -> Path:
    path = settings.manifest_dir / name
    path.write_text("{}\n", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_purge_removes_only_old_unreferenced_inventories(configured: Settings) -> None:
    repo = _repo(configured)
    old = _inventory(configured, "sync-1-attempt-1.json", 8 * DAY)
    recent = _inventory(configured, "sync-2-attempt-1.json", 1 * DAY)
    in_use = _inventory(configured, "sync-3-attempt-1.json", 30 * DAY)
    other = _inventory(configured, "hashcache-files.json", 30 * DAY)

    with Session(get_engine()) as db:
        db.add(
            Job(
                repo_id=repo.id or 0,
                type="export",
                tool="export",
                params={"mode": "incr", "sync_snapshot": str(in_use)},
            )
        )
        db.commit()
        assert export_jobs.purge_sync_inventories(db, configured) == 1

    assert not old.exists()
    assert recent.exists()
    assert in_use.exists()
    assert other.exists()


@pytest.mark.parametrize(
    "value",
    [None, "", "/etc/passwd", "relative/sync-1-attempt-1.json", "sync-x-attempt-1.json"],
)
def test_discard_ignores_values_outside_the_manifest_directory(
    configured: Settings, value: str | None
) -> None:
    assert export_jobs.discard_sync_inventory(configured, value) is False


def test_chained_export_deletes_its_inventory(configured: Settings) -> None:
    repo = _repo(configured)
    sync = Job(id=999, repo_id=repo.id or 0, attempt=1)
    captured = export_engine.capture_sync(configured, repo, sync, _pulse)
    assert captured.exists()

    with Session(get_engine()) as db:
        job = Job(
            repo_id=repo.id or 0,
            type="export",
            tool="export",
            trigger="chain",
            params={"mode": "full", "sync_snapshot": str(captured)},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    claimed = claim.claim_next(worker_id="export-test", tool="export", limit=4)
    assert claimed is not None and claimed.id == job_id
    worker_main._execute(claimed, "export-test", configured, threading.Event())

    with Session(get_engine()) as db:
        finished = db.get(Job, job_id)
        assert finished is not None and finished.status == "succeeded"
    assert not captured.exists()


class _Adapter:
    def interpret_result(
        self, _plan: Any, result: RunResult, started: datetime, finished: datetime
    ) -> SyncResult:
        return SyncResult(
            status=SyncStatus.SUCCEEDED,
            started_at=started,
            finished_at=finished,
            exit_code=result.exit_code,
        )


class _Runner:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def run(self) -> RunResult:
        return RunResult(exit_code=0, cancelled=False)


def _run_sync(configured: Settings, monkeypatch: pytest.MonkeyPatch, chain: bool) -> list[Job]:
    repo = _repo(configured, chain_export=chain)
    monkeypatch.setattr(
        worker_main,
        "_build_sync_plan",
        lambda *_args: (None, _Adapter(), None, datetime.now()),
    )
    monkeypatch.setattr(worker_main, "Runner", _Runner)

    with Session(get_engine()) as db:
        db.add(Job(repo_id=repo.id or 0, type="sync", tool="wget"))
        db.commit()
    claimed = claim.claim_next(worker_id="sync-test", tool="wget", limit=4)
    assert claimed is not None
    worker_main._execute(claimed, "sync-test", configured, threading.Event())

    with Session(get_engine()) as db:
        return list(db.exec(select(Job).order_by(Job.id)).all())  # type: ignore[arg-type]


def test_sync_without_chained_export_writes_no_inventory(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _run_sync(configured, monkeypatch, chain=False)

    assert [job.status for job in jobs] == ["succeeded"]
    assert list(configured.manifest_dir.glob("sync-*.json")) == []


def test_sync_with_chained_export_writes_inventory_for_the_export(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _run_sync(configured, monkeypatch, chain=True)

    assert [job.type for job in jobs] == ["sync", "export"]
    inventories = list(configured.manifest_dir.glob("sync-*.json"))
    assert len(inventories) == 1
    assert jobs[1].params["sync_snapshot"] == str(inventories[0])
