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
    import hashlib

    original = bytes(range(256)) * 20
    writer = pack.PartWriter(tmp_path, "data.tar", 1000)
    for start in range(0, len(original), 333):
        writer.write(original[start : start + 333])
    parts = writer.close()

    assert [path.name for path, _ in parts] == [f"data.tar.{i:05d}" for i in range(6)]
    assert all(path.stat().st_size <= 1000 for path, _ in parts)
    assert b"".join(path.read_bytes() for path, _ in parts) == original
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in parts)


def test_unsplit_writer_writes_one_file(tmp_path: Path) -> None:
    writer = pack.PartWriter(tmp_path, "data.tar", 0)
    writer.write(b"x" * 5000)
    parts = writer.close()
    assert [path.name for path, _ in parts] == ["data.tar"]


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
    import tarfile

    from mirrorhub.export import manifest

    repo = _repository(configured)
    root = configured.data_dir / repo.slug

    # Baseline: modified, deleted, and unchanged files.
    (root / "delete.txt").write_bytes(b"remove me")
    (root / "unchanged.txt").write_bytes(b"keep me")

    manifest.snapshot(root)
    first = engine.execute(_job(repo, "full"), repo, configured, _pulse)

    # Change the source before producing the incremental.
    (root / "first.txt").write_bytes(b"first: modified content")
    (root / "delete.txt").unlink()
    (root / "nested").mkdir()
    (root / "nested" / "added.txt").write_bytes(b"new file")

    manifest.snapshot(root)

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

        archive_path = directory / metadata["archive_parts"][0]

        with tarfile.open(archive_path, "r:") as archive:
            for member in archive:
                manifest.validate_relative(member.name)
                assert member.isfile()
                assert member.name in inventory


def test_full_export_hashes_while_packing(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full export reads every payload file once: its inventory is built from stat
    data and the digests come from packing. The result equals a hashed inventory."""
    from mirrorhub.export import manifest

    repo = _repository(configured)
    root = configured.data_dir / repo.slug
    (root / "nested").mkdir()
    (root / "nested" / "b.bin").write_bytes(b"b" * 3_000_000)
    (root / "link").symlink_to("first.txt")

    reads: list[str] = []
    real_hash = manifest.file_hash

    def counting_hash(path: Path, pulse: object = None) -> str:
        reads.append(path.name)
        return real_hash(path)

    monkeypatch.setattr(manifest, "file_hash", counting_hash)
    # Treat the new files as old enough to cache, so the post-check uses the cache.
    real_snapshot = manifest.snapshot

    def snapshot(*args: object, **kwargs: object) -> manifest.Inventory:
        kwargs.setdefault("racy_margin_ns", 0)
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(manifest, "snapshot", snapshot)

    identifier = engine.execute(_job(repo, "full"), repo, configured, _pulse)
    directory = configured.export_dir / repo.slug / identifier

    assert "first.txt" not in reads and "b.bin" not in reads
    monkeypatch.setattr(manifest, "file_hash", real_hash)
    inventory = json.loads((directory / "files.json").read_text())
    assert inventory == real_snapshot(root)
    verify(directory)
    cache = manifest.load_hash_cache(engine.hash_cache_path(configured, repo.slug))
    assert cache["nested/b.bin"][4] == inventory["nested/b.bin"]["sha256"]


def test_full_export_with_stale_sync_snapshot_is_refused(configured: Settings) -> None:
    repo = _repository(configured)
    sync = Job(id=998, repo_id=repo.id or 0, attempt=1)
    captured = engine.capture_sync(configured, repo, sync, _pulse)
    (configured.data_dir / repo.slug / "first.txt").write_bytes(b"other")  # same size

    job = _job(repo, "full")
    job.params = {"mode": "full", "sync_snapshot": str(captured)}
    with pytest.raises(ExportError, match="changed after sync"):
        engine.execute(job, repo, configured, _pulse)
    assert list(configured.export_dir.rglob(".done")) == []


def test_full_export_with_matching_sync_snapshot(configured: Settings) -> None:
    repo = _repository(configured)
    sync = Job(id=997, repo_id=repo.id or 0, attempt=1)
    captured = engine.capture_sync(configured, repo, sync, _pulse)
    job = _job(repo, "full")
    job.params = {"mode": "full", "sync_snapshot": str(captured)}
    identifier = engine.execute(job, repo, configured, _pulse)
    verify(configured.export_dir / repo.slug / identifier)


def test_changed_content_with_same_stat_is_detected_while_packing(
    configured: Settings, tmp_path: Path
) -> None:
    from mirrorhub.export import manifest

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_bytes(b"aaaa")
    inventory = manifest.snapshot(root)
    inventory["a.txt"]["sha256"] = "0" * 64
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ExportError, match="changed before packing"):
        pack.write_archive(root, out, inventory, ["a.txt"], _pulse, compress=False, part_bytes=0)


def test_zstd_export_round_trip(configured: Settings, tmp_path: Path) -> None:
    import shutil
    import subprocess
    import tarfile

    if shutil.which("zstd") is None:
        pytest.skip("zstd not installed")
    root = tmp_path / "repo"
    root.mkdir()
    import os

    content = os.urandom(50_000)  # incompressible: the archive spans several parts
    (root / "a.txt").write_bytes(content)
    from mirrorhub.export import manifest

    inventory = manifest.snapshot(root)
    out = tmp_path / "out"
    out.mkdir()
    parts, digests = pack.write_archive(
        root, out, inventory, ["a.txt"], _pulse, compress=True, part_bytes=10_000
    )
    assert len(parts) > 1
    assert digests["a.txt"] == inventory["a.txt"]["sha256"]
    joined = b"".join(path.read_bytes() for path, _ in parts)
    raw = subprocess.run(["zstd", "-d", "-c"], input=joined, capture_output=True, check=True)
    archive = tmp_path / "data.tar"
    archive.write_bytes(raw.stdout)
    with tarfile.open(archive) as tar:
        member = tar.extractfile("a.txt")
        assert member is not None and member.read() == content


def test_parallel_inventory_matches_sequential(tmp_path: Path) -> None:
    from mirrorhub.export import manifest

    for directory in range(5):
        (tmp_path / f"d{directory}").mkdir()
        for name in range(40):
            (tmp_path / f"d{directory}" / f"f{name}").write_bytes(f"{directory}-{name}".encode())
    (tmp_path / "d0" / "link").symlink_to("f1")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "skipped").write_bytes(b"x")
    sequential = manifest.snapshot(tmp_path)
    assert manifest.snapshot(tmp_path, threads=8) == sequential
    assert ".tmp/skipped" not in sequential
    assert list(sequential) == sorted(sequential)
