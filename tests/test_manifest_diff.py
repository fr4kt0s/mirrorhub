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
