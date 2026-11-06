"""Create a consistent SQLite backup using the standard library."""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote


def backup(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.absolute()

    if destination.exists() or destination.is_symlink():
        raise FileExistsError("backup destination already exists")
    if source == destination:
        raise ValueError("source and backup destination must differ")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mirrorhub-backup-",
        suffix=".sqlite3",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
        with (
            contextlib.closing(sqlite3.connect(uri, uri=True, timeout=30)) as original,
            contextlib.closing(sqlite3.connect(temporary, timeout=30)) as copied,
        ):
            original.backup(copied, pages=256, sleep=0.1)
            result = copied.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError("backup failed SQLite integrity_check")

        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())

        os.link(temporary, destination)
        temporary.unlink()

        directory = os.open(
            destination.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    backup(arguments.source, arguments.destination)
    print("SQLite backup completed and integrity-checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
