"""Export metadata copies on the state volume and drop-folder housekeeping."""

import os
import shutil
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.export import job as engine
from mirrorhub.export import metadata_store
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Job, Repository
from mirrorhub.services import appsettings, export_jobs
from mirrorhub.settings import Settings
from mirrorhub.verify import verify


def _pulse(_pct: float | None, _text: str) -> None:
    pass


def _repository(settings: Settings) -> Repository:
    with Session(get_engine()) as db:
        repo = Repository(slug="files", name="Files", kind="generic", tool="wget")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        detached = Repository(**repo.model_dump())
    root = settings.data_dir / repo.slug
    root.mkdir()
    (root / "first.txt").write_bytes(b"first")
    return detached


def _export(repo: Repository, settings: Settings, mode: str = "incr") -> str:
    with Session(get_engine()) as db:
        row = Job(repo_id=repo.id or 0, type="export", tool="export", params={"mode": mode})
        db.add(row)
        db.commit()
        db.refresh(row)
        job = Job(**row.model_dump())
    time.sleep(1.05)  # export ids have one-second resolution
    return engine.execute(job, repo, settings, _pulse)


def _change(settings: Settings) -> None:
    (settings.data_dir / "files" / "second.txt").write_bytes(b"second")


def test_publish_stores_metadata_without_archive(configured: Settings) -> None:
    repo = _repository(configured)
    export_id = _export(repo, configured)

    stored = metadata_store.directory(configured, repo.slug, export_id)
    names = {path.name for path in stored.iterdir()}
    assert {"files.json", "manifest.json", "MANIFEST.sha256", "deleted.txt"} <= names
    assert not any(name.startswith("data.tar") for name in names)


def test_incremental_works_after_pickup_moved_the_export(
    configured: Settings, tmp_path: Path
) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    shutil.move(configured.export_dir / repo.slug / first, tmp_path / first)

    _change(configured)
    second = _export(repo, configured)

    metadata = verify(configured.export_dir / repo.slug / second)
    assert metadata["mode"] == "incr"
    assert metadata["base_export_id"] == first
    assert metadata["payload_paths"] == ["second.txt"]


def test_incremental_does_not_need_the_previous_archive(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    for part in (configured.export_dir / repo.slug / first).glob("data.tar*"):
        part.unlink()

    _change(configured)
    second = _export(repo, configured)
    assert verify(configured.export_dir / repo.slug / second)["base_export_id"] == first


def test_fallback_to_drop_folder_backfills_the_copy(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    stored = metadata_store.directory(configured, repo.slug, first)
    shutil.rmtree(stored)

    _change(configured)
    second = _export(repo, configured)

    assert stored.is_dir()
    assert verify(configured.export_dir / repo.slug / second)["base_export_id"] == first


def test_missing_base_asks_for_a_full_export(configured: Settings, tmp_path: Path) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    shutil.rmtree(metadata_store.directory(configured, repo.slug, first))
    shutil.move(configured.export_dir / repo.slug / first, tmp_path / first)

    _change(configured)
    with pytest.raises(ExportError, match="run a full export"):
        _export(repo, configured)

    full = _export(repo, configured, mode="full")
    assert verify(configured.export_dir / repo.slug / full)["mode"] == "full"


def test_tampered_metadata_copy_is_rejected(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    stored = metadata_store.directory(configured, repo.slug, first)
    (stored / "files.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ExportError, match=r"checksum mismatch for files\.json"):
        metadata_store.load_base(configured, repo.slug, first)


def test_prune_keeps_newest_and_protected(configured: Settings) -> None:
    base = metadata_store.root(configured) / "files"
    ids = [f"2026010{day}T000000Z-incr" for day in range(1, 6)]
    for export_id in ids:
        (base / export_id).mkdir(parents=True)

    removed = metadata_store.prune(configured, {"files": ids[0]}, keep=2)

    assert removed == 2
    assert sorted(path.name for path in base.iterdir()) == [ids[0], ids[3], ids[4]]


def test_prune_removes_only_stale_tmp(configured: Settings) -> None:
    base = metadata_store.root(configured) / "files"
    stale = base / "20260101T000000Z-full.tmp"
    fresh = base / "20260102T000000Z-full.tmp"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = time.time() - 2 * metadata_store.STALE_TMP_S
    os.utime(stale, (old, old))

    metadata_store.prune(configured, {})
    assert not stale.exists()
    assert fresh.exists()


def test_clean_drop_folder(configured: Settings) -> None:
    repo = _repository(configured)
    parent = configured.export_dir / repo.slug
    empty = parent / "20260101T000000Z-full"
    leftovers = parent / "20260102T000000Z-incr"
    recent = parent / "20260103T000000Z-incr"
    complete = parent / "20260104T000000Z-incr"
    running = parent / "20260105T000000Z-incr.tmp"
    for directory in (empty, leftovers, recent, complete, running):
        directory.mkdir(parents=True)
    for directory in (leftovers, recent, complete):
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        (directory / ".done").write_text("x", encoding="ascii")
    (complete / "data.tar").write_bytes(b"archive")
    old = time.time() - 2 * export_jobs.PICKED_UP_GRACE_S
    os.utime(leftovers, (old, old))
    os.utime(complete, (old, old))

    with Session(get_engine()) as db:
        removed = export_jobs.clean_drop_folder(db, configured)

    assert sorted(removed) == [f"files/{empty.name}", f"files/{leftovers.name}"]
    assert recent.exists()  # pickup may still be moving files
    assert complete.exists()  # archive not picked up yet
    assert running.exists()  # export in progress


def test_clean_drop_folder_removes_empty_repository_directory(configured: Settings) -> None:
    _repository(configured)
    parent = configured.export_dir / "files"
    parent.mkdir(parents=True)

    with Session(get_engine()) as db:
        assert export_jobs.clean_drop_folder(db, configured) == ["files/"]
    assert not parent.exists()


def test_drop_folder_state(configured: Settings) -> None:
    repo = _repository(configured)
    export_id = _export(repo, configured)
    assert export_jobs.drop_folder_state(configured, repo.slug, export_id) == "present"

    (configured.export_dir / repo.slug / export_id / ".done").unlink()
    assert export_jobs.drop_folder_state(configured, repo.slug, export_id) == "partial"

    shutil.rmtree(configured.export_dir / repo.slug / export_id)
    assert export_jobs.drop_folder_state(configured, repo.slug, export_id) == "picked up"


def test_retention_removes_metadata_copies(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured, mode="full")
    second = _export(repo, configured, mode="full")

    with Session(get_engine()) as db:
        appsettings.set_value(db, export_jobs.RETENTION_KEY, 1)
        removed, _freed = export_jobs.apply_retention(db, configured, actor="test")

    assert removed == [f"files/{first}"]
    assert not metadata_store.directory(configured, repo.slug, first).exists()
    assert metadata_store.directory(configured, repo.slug, second).is_dir()
