"""Export base from the metadata copies (markers, reconcile) and "incremental since"."""

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.export import job as engine
from mirrorhub.export import metadata_store, reconcile
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import export_jobs
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


def _export(repo: Repository, settings: Settings, **params: Any) -> str:
    params.setdefault("mode", "incr")
    with Session(get_engine()) as db:
        row = Job(repo_id=repo.id or 0, type="export", tool="export", params=params)
        db.add(row)
        db.commit()
        db.refresh(row)
        job = Job(**row.model_dump())
    time.sleep(1.05)  # export ids have one-second resolution
    return engine.execute(job, repo, settings, _pulse)


def _write(settings: Settings, name: str, data: bytes) -> None:
    (settings.data_dir / "files" / name).write_bytes(data)


def _rows() -> list[Export]:
    with Session(get_engine()) as db:
        return list(db.exec(select(Export).order_by(col(Export.id))).all())


def _drop_export_rows() -> None:
    with Session(get_engine()) as db:
        for row in db.exec(select(Export)).all():
            db.delete(row)
        db.commit()


def _marker(settings: Settings, export_id: str) -> Path:
    return metadata_store.directory(settings, "files", export_id) / metadata_store.PUBLISHED_MARKER


# Part A: markers and base selection


def test_publication_writes_a_marker(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _write(configured, "second.txt", b"second")
    second = _export(repo, configured)

    marker = json.loads(_marker(configured, second).read_text("utf-8"))
    assert marker["export_id"] == second
    assert marker["base_export_id"] == first
    assert marker["mode"] == "incr"
    assert metadata_store.latest_published(configured, "files") == second


def test_lost_database_does_not_force_a_full_export(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _drop_export_rows()

    _write(configured, "second.txt", b"second")
    second = _export(repo, configured)

    metadata = verify(configured.export_dir / "files" / second)
    assert metadata["mode"] == "incr"
    assert metadata["base_export_id"] == first
    assert metadata["payload_paths"] == ["second.txt"]
    assert [row.export_id for row in _rows()] == [first, second]
    restored = _rows()[0]
    assert restored.status == "published" and restored.job_id is None


def test_copy_without_marker_is_never_a_base(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _write(configured, "second.txt", b"second")
    second = _export(repo, configured)
    # As if publication had failed after storing the copy: no marker, no row, no .done.
    _marker(configured, second).unlink()
    shutil.rmtree(configured.export_dir / "files" / second)
    with Session(get_engine()) as db:
        row = db.exec(select(Export).where(Export.export_id == second)).one()
        db.delete(row)
        db.commit()

    third = _export(repo, configured)
    assert verify(configured.export_dir / "files" / third)["base_export_id"] == first


def test_database_newer_than_copies_stops(configured: Settings) -> None:
    repo = _repository(configured)
    _export(repo, configured)
    _write(configured, "second.txt", b"second")
    second = _export(repo, configured)
    shutil.rmtree(metadata_store.directory(configured, "files", second))
    shutil.rmtree(configured.export_dir / "files" / second)

    _write(configured, "third.txt", b"third")
    with pytest.raises(ExportError, match=r"run a full export.*since an earlier export"):
        _export(repo, configured)


def test_existing_copies_get_markers(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _marker(configured, first).unlink()

    with Session(get_engine()) as db:
        export_jobs.housekeeping(db, configured)

    assert _marker(configured, first).is_file()


def test_reconcile_dry_run_and_cli(
    configured: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _drop_export_rows()

    with Session(get_engine()) as db:
        assert reconcile.reconcile(db, configured, dry_run=True) == [f"files/{first}"]
    assert _rows() == []

    assert reconcile.main(["--slug", "files"]) == 0
    assert f"created files/{first}" in capsys.readouterr().out
    assert [row.export_id for row in _rows()] == [first]
    assert reconcile.main([]) == 0  # idempotent
    assert len(_rows()) == 1


def _fake_copy(settings: Settings, export_id: str, created_at: str, base: str | None) -> None:
    directory = metadata_store.directory(settings, "files", export_id)
    directory.mkdir(parents=True)
    (directory / metadata_store.PUBLISHED_MARKER).write_text(
        json.dumps(
            {
                "export_id": export_id,
                "created_at": created_at,
                "mode": "incr" if base else "full",
                "base_export_id": base,
            }
        ),
        encoding="utf-8",
    )


def test_latest_follows_base_links_and_survives_clock_jumps(configured: Settings) -> None:
    _fake_copy(configured, "20260101T000000Z-full", "2026-01-01T00:00:00+00:00", None)
    _fake_copy(
        configured,
        "20260102T000000Z-incr",
        "2026-01-02T00:00:00+00:00",
        "20260101T000000Z-full",
    )
    # Created after a clock jump back, but built on the export of 2 January.
    _fake_copy(
        configured,
        "20260101T120000Z-incr",
        "2026-01-01T12:00:00+00:00",
        "20260102T000000Z-incr",
    )
    assert metadata_store.latest_published(configured, "files") == "20260101T120000Z-incr"


def test_housekeeping_keeps_the_configured_number_of_copies(configured: Settings) -> None:
    for day in range(1, 6):
        (metadata_store.root(configured) / "files" / f"2026010{day}T000000Z-incr").mkdir(
            parents=True
        )
    settings = configured.model_copy(update={"export_meta_keep": 2})

    with Session(get_engine()) as db:
        result = export_jobs.housekeeping(db, settings)

    assert result["metadata_pruned"] == 3
    remaining = sorted(path.name for path in (metadata_store.root(configured) / "files").iterdir())
    assert remaining == ["20260104T000000Z-incr", "20260105T000000Z-incr"]


# Part B: incremental export since a chosen base


def test_incremental_since_an_earlier_export(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _write(configured, "second.txt", b"second")
    _export(repo, configured)
    _write(configured, "third.txt", b"third")
    (configured.data_dir / "files" / "first.txt").unlink()
    third = _export(repo, configured)

    since = _export(repo, configured, base=first)

    metadata = verify(configured.export_dir / "files" / since)
    assert metadata["mode"] == "incr"
    assert metadata["base_export_id"] == first
    assert metadata["payload_paths"] == ["second.txt", "third.txt"]
    deleted = (configured.export_dir / "files" / since / "deleted.txt").read_text("utf-8")
    assert deleted == "first.txt\n"
    assert metadata_store.latest_published(configured, "files") == since

    rows = _rows()
    skipped = export_jobs.superseded(rows)
    assert {row.export_id for row in rows if row.id in skipped} == {
        row.export_id for row in rows if first < row.export_id <= third
    }


def test_enqueue_checks_the_chosen_base(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    repo_id = repo.id or 0

    with pytest.raises(ExportError, match="no published metadata copy"):
        export_jobs.enqueue(
            repo_id, mode="incr", actor="test", base_export_id="20200101T000000Z-incr"
        )
    with pytest.raises(ExportError, match="always incremental"):
        export_jobs.enqueue(repo_id, mode="full", actor="test", base_export_id=first)
    with pytest.raises(ExportError, match="invalid base"):
        export_jobs.enqueue(repo_id, mode="incr", actor="test", base_export_id="../x")

    job_id = export_jobs.enqueue(repo_id, mode="incr", actor="test", base_export_id=first)
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.params == {"mode": "incr", "base": first}
    with pytest.raises(ExportError, match="already queued or running"):
        export_jobs.enqueue(repo_id, mode="incr", actor="test", base_export_id=first)


def test_chosen_base_without_marker_is_refused_at_run_time(configured: Settings) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)
    _marker(configured, first).unlink()
    shutil.rmtree(configured.export_dir / "files" / first)
    with Session(get_engine()) as db:
        db.delete(db.exec(select(Export)).one())
        db.commit()

    with pytest.raises(ExportError, match="no published metadata copy"):
        _export(repo, configured, base=first)


def test_exports_page_offers_incremental_since(
    admin: TestClient, csrf_token: str, configured: Settings
) -> None:
    repo = _repository(configured)
    first = _export(repo, configured)

    page = admin.get("/exports")
    assert page.status_code == 200
    assert "Incremental since" in page.text
    assert first in page.text

    headers = {"X-CSRF-Token": csrf_token}
    refused = admin.post(
        f"/exports/{repo.id}/start", data={"mode": "full", "base": first}, headers=headers
    )
    assert refused.status_code == 400

    started = admin.post(
        f"/exports/{repo.id}/start", data={"mode": "incr", "base": first}, headers=headers
    )
    assert started.status_code == 303
    with Session(get_engine()) as db:
        job = db.exec(select(Job).where(Job.type == "export")).one()
        assert job.params == {"mode": "incr", "base": first}


def test_round_trip_through_the_importer(configured: Settings, tmp_path: Path) -> None:
    """Sender: full, two incrementals, one since the full. Receiver: import --slug
    applies the full, skips the two incrementals as superseded, applies the last."""
    from mirrorhub.importer.cli import main as importer
    from mirrorhub.importer.state import StateStore

    repo = _repository(configured)
    first = _export(repo, configured)
    _write(configured, "second.txt", b"second")
    second = _export(repo, configured)
    _write(configured, "third.txt", b"third")
    (configured.data_dir / "files" / "first.txt").unlink()
    third = _export(repo, configured)
    since = _export(repo, configured, base=first)

    receiver = tmp_path / "receiver"
    receiver.mkdir()
    config = receiver / "importer.toml"
    config.write_text(
        f'export_dir = "{configured.export_dir}"\nmirror_root = "mirror"\nstate_dir = "state"\n',
        encoding="utf-8",
    )
    assert importer(["--config", str(config), "import", "--slug", "files"]) == 0

    tree = receiver / "mirror" / "files"
    assert sorted(path.name for path in tree.iterdir()) == ["second.txt", "third.txt"]
    assert (tree / "third.txt").read_bytes() == b"third"
    state = StateStore(receiver / "state", "files").load()
    assert state.last_export_id == since
    assert state.applied(first) and state.applied(since)
    assert state.superseded(second) and state.superseded(third)
