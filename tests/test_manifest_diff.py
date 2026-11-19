"""File inventory and incremental diff tests."""

import os
from pathlib import Path

import pytest

from mirrorhub.export import manifest


def test_content_change_with_same_size_and_mtime_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "package.deb"
    path.write_bytes(b"first")
    previous = manifest.snapshot(tmp_path)
    original = path.stat()

    path.write_bytes(b"other")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    current = manifest.snapshot(tmp_path)

    changed, deleted = manifest.diff(current, previous)
    assert changed == ["package.deb"]
    assert deleted == []


def test_diff_detects_addition_and_deletion(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.write_bytes(b"old")
    previous = manifest.snapshot(tmp_path)
    old.unlink()
    (tmp_path / "new").write_bytes(b"new")

    changed, deleted = manifest.diff(manifest.snapshot(tmp_path), previous)
    assert changed == ["new"]
    assert deleted == ["old"]


def test_unchanged_inventory_has_empty_diff(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"content")
    current = manifest.snapshot(tmp_path)
    assert manifest.diff(current, current) == ([], [])


def test_safe_symlink_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "database.tar.gz").write_bytes(b"database")
    (tmp_path / "database.db").symlink_to("database.tar.gz")
    inventory = manifest.snapshot(tmp_path)
    assert inventory["database.db"]["type"] == "symlink"
    assert inventory["database.db"]["target"] == "database.tar.gz"


def test_escaping_symlink_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "escape").symlink_to("../outside")
    with pytest.raises(manifest.ExportError, match="escapes"):
        manifest.snapshot(tmp_path)


@pytest.mark.parametrize("value", ["../x", "/etc/passwd", "a\nb", "a/../b"])
def test_unsafe_paths_are_rejected(value: str) -> None:
    with pytest.raises(manifest.ExportError):
        manifest.validate_relative(value)


def _counting_hash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = manifest.file_hash

    def counting(path: Path, pulse: manifest.Pulse | None = None) -> str:
        calls.append(path.name)
        return original(path, pulse)

    monkeypatch.setattr(manifest, "file_hash", counting)
    return calls


def test_cache_reuses_hashes_for_unchanged_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.deb").write_bytes(b"alpha")
    (tmp_path / "b.deb").write_bytes(b"bravo")
    calls = _counting_hash(monkeypatch)
    cache: manifest.HashCache = {}

    first = manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)
    assert sorted(calls) == ["a.deb", "b.deb"]
    assert set(cache) == {"a.deb", "b.deb"}

    calls.clear()
    second = manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)
    assert calls == []
    assert second == first
    assert second == manifest.snapshot(tmp_path)


def test_cache_rehashes_changed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "package.deb"
    path.write_bytes(b"first")
    cache: manifest.HashCache = {}
    previous = manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)

    path.write_bytes(b"second version")
    calls = _counting_hash(monkeypatch)
    current = manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)

    assert calls == ["package.deb"]
    assert manifest.diff(current, previous) == (["package.deb"], [])
    assert current == manifest.snapshot(tmp_path)


def test_same_size_and_mtime_rewrite_is_detected_with_cache(tmp_path: Path) -> None:
    path = tmp_path / "package.deb"
    path.write_bytes(b"first")
    cache: manifest.HashCache = {}
    previous = manifest.snapshot(tmp_path, cache=cache)
    original = path.stat()

    path.write_bytes(b"other")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    current = manifest.snapshot(tmp_path, cache=cache)

    assert manifest.diff(current, previous) == (["package.deb"], [])


def test_recently_changed_files_are_not_cached(tmp_path: Path) -> None:
    (tmp_path / "fresh.deb").write_bytes(b"fresh")
    cache: manifest.HashCache = {}
    manifest.snapshot(tmp_path, cache=cache)
    assert cache == {}


def test_cache_drops_deleted_paths(tmp_path: Path) -> None:
    (tmp_path / "keep").write_bytes(b"keep")
    (tmp_path / "gone").write_bytes(b"gone")
    cache: manifest.HashCache = {}
    manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)
    (tmp_path / "gone").unlink()

    manifest.snapshot(tmp_path, cache=cache, racy_margin_ns=0)
    assert set(cache) == {"keep"}


def test_hash_cache_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file").write_bytes(b"content")
    cache: manifest.HashCache = {}
    manifest.snapshot(root, cache=cache, racy_margin_ns=0)

    path = tmp_path / "state" / "hashcache-repo.json"
    manifest.save_hash_cache(path, cache)
    assert manifest.load_hash_cache(path) == cache
    assert not path.with_name(f"{path.name}.tmp").exists()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 99, "entries": {}}',
        '{"version": 1, "entries": []}',
    ],
)
def test_malformed_hash_cache_is_treated_as_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "hashcache.json"
    path.write_text(content, encoding="utf-8")
    assert manifest.load_hash_cache(path) == {}


def test_invalid_hash_cache_entries_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "hashcache.json"
    path.write_text(
        '{"version": 1, "entries": {'
        '"ok": [1, 2, 3, 4, "' + "a" * 64 + '"], '
        '"short": [1, 2, 3, 4], '
        '"bad-digest": [1, 2, 3, 4, "xyz"]}}',
        encoding="utf-8",
    )
    assert set(manifest.load_hash_cache(path)) == {"ok"}


def test_missing_hash_cache_is_empty(tmp_path: Path) -> None:
    assert manifest.load_hash_cache(tmp_path / "missing.json") == {}
