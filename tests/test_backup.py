"""Consistent SQLite backups and overwrite protection."""

import sqlite3
from pathlib import Path

import pytest

from mirrorhub.backup import backup


def test_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"

    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE example (value TEXT NOT NULL)")
        connection.execute("INSERT INTO example VALUES ('preserved')")
        connection.commit()
        backup(source, destination)

    with sqlite3.connect(destination) as copied:
        assert copied.execute("SELECT value FROM example").fetchone() == ("preserved",)
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"

    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example (id INTEGER)")

    destination.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        backup(source, destination)
    assert destination.read_bytes() == b"keep me"
