"""Repository inventories and incremental diffing."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

Inventory = dict[str, dict[str, Any]]
Pulse = Callable[[float | None, str], None]

SKIP_DIRECTORIES = frozenset({".temp", ".tmp", ".mirrorhub", ".rsync-partial"})


class ExportError(RuntimeError):
    """An export cannot be produced safely."""


def validate_relative(value: str) -> None:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(character) < 32 for character in value)
        or "\\" in value
    ):
        raise ExportError(f"unsafe relative path: {value!r}")


def validate_link(path: str, target: str) -> None:
    if (
        not target
        or target.startswith("/")
        or "\\" in target
        or any(ord(character) < 32 for character in target)
    ):
        raise ExportError(f"unsafe symlink: {path!r}")

    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ExportError(f"symlink escapes repository: {path!r}")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def file_hash(path: Path, pulse: Pulse | None = None) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)

    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ExportError(f"not a regular file: {path.name}")

        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            if pulse is not None:
                pulse(None, f"Hashing {path.name}")

        after = os.fstat(handle.fileno())

    if _identity(before) != _identity(after):
        raise ExportError(f"file changed while hashing: {path.name}")

    return digest.hexdigest()


def _walk_error(error: OSError) -> None:
    raise ExportError(f"cannot scan repository directory: {error.filename}") from error


def snapshot(root: Path, pulse: Pulse | None = None) -> Inventory:
    if root.is_symlink() or not root.is_dir():
        raise ExportError("repository directory is missing or is a symlink")

    inventory: Inventory = {}

    for directory, names, filenames in os.walk(
        root,
        followlinks=False,
        onerror=_walk_error,
    ):
        names.sort()
        filenames.sort()

        directory_path = Path(directory)
        links = [name for name in names if (directory_path / name).is_symlink()]
        names[:] = [name for name in names if name not in SKIP_DIRECTORIES and name not in links]

        for name in sorted([*filenames, *links]):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            validate_relative(relative)
            before = path.lstat()

            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(path)
                validate_link(relative, target)
                after = path.lstat()

                if _identity(before) != _identity(after):
                    raise ExportError(f"symlink changed during inventory: {relative}")

                inventory[relative] = {
                    "type": "symlink",
                    "target": target,
                    "size": before.st_size,
                    "mtime_ns": before.st_mtime_ns,
                    "sha256": hashlib.sha256(os.fsencode(target)).hexdigest(),
                }
            elif stat.S_ISREG(before.st_mode):
                digest = file_hash(path, pulse)
                after = path.lstat()

                if _identity(before) != _identity(after):
                    raise ExportError(f"file changed during inventory: {relative}")

                inventory[relative] = {
                    "type": "file",
                    "size": before.st_size,
                    "mtime_ns": before.st_mtime_ns,
                    "sha256": digest,
                }
            else:
                raise ExportError(f"unsupported filesystem entry: {relative}")

            if pulse is not None:
                pulse(None, f"Inventoried {len(inventory)} entries")

    return dict(sorted(inventory.items()))


def diff(
    current: Inventory,
    previous: Inventory,
) -> tuple[list[str], list[str]]:
    changed = sorted(path for path, entry in current.items() if previous.get(path) != entry)
    deleted = sorted(set(previous) - set(current))
    return changed, deleted


def save(path: Path, inventory: Inventory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("x", encoding="utf-8") as handle:
        json.dump(
            inventory,
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def load(path: Path) -> Inventory:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)

    if not isinstance(value, dict):
        raise ExportError("inventory must be a mapping")

    for relative, entry in value.items():
        if not isinstance(relative, str):
            raise ExportError("inventory paths must be strings")
        validate_relative(relative)

        if not isinstance(entry, dict):
            raise ExportError("invalid inventory entry")

    return value
