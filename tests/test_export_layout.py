"""Export layout, incrementals, checksums and archive splitting."""

import json
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.export import job as engine
from mirrorhub.export import pack
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Job, Repository
from mirrorhub.settings import Settings
from mirrorhub.verify import VerificationError, verify


def _pulse(_pct: float | None, _text: str) -> None:
    pass


def _repository(settings: Settings) -> Repository:
    repo = Repository(
        slug="files",
        name="Files",
        kind="generic",
        tool="wget",
        export_format="tar",
    )
    with Session(get_engine()) as db:
        db.add(repo)
        db.commit()
        db.refresh(repo)
        detached = Repository(**repo.model_dump())

    root = settings.data_dir / repo.slug
    root.mkdir()
    (root / "first.txt").write_bytes(b"first")
    return detached


def _job(repo: Repository, mode: str = "incr") -> Job:
    with Session(get_engine()) as db:
        row = Job(
            repo_id=repo.id or 0,
            type="export",
            tool="export",
            params={"mode": mode},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return Job(**row.model_dump())


def test_first_incremental_becomes_full(configured: Settings) -> None:
    repo = _repository(configured)
    identifier = engine.execute(_job(repo), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / identifier

    assert (directory / ".done").is_file()
    assert not directory.with_name(f"{identifier}.tmp").exists()
    metadata = verify(directory)
    assert metadata["mode"] == "full"
    assert metadata["base_export_id"] is None
    assert metadata["counts"]["payload"] == 1


def test_incremental_contains_changes_and_deletions(configured: Settings) -> None:
    repo = _repository(configured)
    first = engine.execute(_job(repo), repo, configured, _pulse)
    root = configured.data_dir / repo.slug
    (root / "first.txt").unlink()
    (root / "second.txt").write_bytes(b"second")

    time.sleep(1.05)
    second = engine.execute(_job(repo), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / second
    metadata = verify(directory)

    assert metadata["mode"] == "incr"
    assert metadata["base_export_id"] == first
    assert metadata["payload_paths"] == ["second.txt"]
    assert (directory / "deleted.txt").read_text() == "first.txt\n"
    assert (configured.export_dir / repo.slug / first / ".done").is_file()


def test_tampered_archive_is_rejected(configured: Settings) -> None:
    repo = _repository(configured)
    identifier = engine.execute(_job(repo), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / identifier
    with (directory / "data.tar").open("ab") as handle:
        handle.write(b"corruption")

    with pytest.raises(VerificationError, match="checksum mismatch"):
        verify(directory)


def test_missing_done_marker_is_rejected(configured: Settings) -> None:
    repo = _repository(configured)
    identifier = engine.execute(_job(repo), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / identifier
    (directory / ".done").unlink()

    with pytest.raises(VerificationError, match="required"):
        verify(directory)


def test_split_parts_reconstruct_original(tmp_path: Path) -> None:
    original = bytes(range(256)) * 20
    source = tmp_path / "data.tar"
    source.write_bytes(original)
    parts = pack.split(source, 1000, _pulse)

    assert not source.exists()
    assert all(path.stat().st_size <= 1000 for path in parts)
    assert b"".join(path.read_bytes() for path in parts) == original


def test_stale_sync_snapshot_refuses_publication(configured: Settings) -> None:
    repo = _repository(configured)
    sync = Job(id=999, repo_id=repo.id or 0, attempt=1)
    captured = engine.capture_sync(configured, repo, sync, _pulse)
    (configured.data_dir / repo.slug / "first.txt").write_bytes(b"changed")

    job = _job(repo)
    job.params = {"mode": "incr", "sync_snapshot": str(captured)}
    with pytest.raises(ExportError, match="changed after sync"):
        engine.execute(job, repo, configured, _pulse)

    assert list(configured.export_dir.rglob(".done")) == []


def test_inventory_is_covered_by_checksums(configured: Settings) -> None:
    repo = _repository(configured)
    identifier = engine.execute(_job(repo), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / identifier
    checksums = (directory / "MANIFEST.sha256").read_text()
    assert "  files.json\n" in checksums
    assert "  manifest.json\n" in checksums
    assert "  deleted.txt\n" in checksums
    inventory = json.loads((directory / "files.json").read_text())
    assert inventory["first.txt"]["size"] == 5


def test_full_and_incremental_restore_round_trip(
    configured: Settings,
    tmp_path: Path,
) -> None:
    import os
    import shutil
    import tarfile

    from mirrorhub.export import manifest

    repo = _repository(configured)
    root = configured.data_dir / repo.slug

    # Baseline: modified, deleted, and unchanged files.
    (root / "delete.txt").write_bytes(b"remove me")
    (root / "unchanged.txt").write_bytes(b"keep me")

    baseline = manifest.snapshot(root)
    first = engine.execute(_job(repo, "full"), repo, configured, _pulse)
    first_directory = configured.export_dir / repo.slug / first

    # Change the source before producing the incremental.
    (root / "first.txt").write_bytes(b"first: modified content")
    (root / "delete.txt").unlink()
    (root / "nested").mkdir()
    (root / "nested" / "added.txt").write_bytes(b"new file")

    expected = manifest.snapshot(root)

    # Match the existing tests' spacing between export identifiers.
    time.sleep(1.05)
    second = engine.execute(_job(repo), repo, configured, _pulse)
    second_directory = configured.export_dir / repo.slug / second

    metadata = verify(second_directory)
    assert metadata["mode"] == "incr"
    assert metadata["base_export_id"] == first
    assert metadata["payload_paths"] == [
        "first.txt",
        "nested/added.txt",
    ]
    assert ((second_directory / "deleted.txt").read_text(encoding="utf-8").splitlines()) == [
        "delete.txt"
    ]

    restored = tmp_path / "restored"
    restored.mkdir()

    def apply_regular_file_export(directory: Path) -> None:
        """Test-only restore for unsplit, uncompressed regular-file exports."""
        metadata = verify(directory)
        inventory = manifest.load(directory / "files.json")

        assert metadata["archive_format"] == "tar"
        assert len(metadata["archive_parts"]) == 1
        assert all(entry["type"] == "file" for entry in inventory.values())

        deleted = (directory / "deleted.txt").read_text(encoding="utf-8").splitlines()
        for relative in deleted:
            manifest.validate_relative(relative)
            target = restored / relative
            assert target.is_file()
            assert not target.is_symlink()
            target.unlink()

        seen = set()
        archive_path = directory / metadata["archive_parts"][0]

        with tarfile.open(archive_path, "r:") as archive:
            for member in archive:
                manifest.validate_relative(member.name)
                assert member.isfile()
                assert member.name in metada
