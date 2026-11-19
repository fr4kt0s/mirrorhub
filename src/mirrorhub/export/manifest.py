"""Repository inventories and incremental diffing."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Inventory = dict[str, dict[str, Any]]
Pulse = Callable[[float | None, str], None]

# Maps a relative path to [st_ino, st_size, st_mtime_ns, st_ctime_ns, sha256].
HashCache = dict[str, list[Any]]

SKIP_DIRECTORIES = frozenset({".temp", ".tmp", ".mirrorhub", ".rsync-partial"})

HASH_CACHE_VERSION = 1

# Files whose ctime or mtime falls within this margin before the snapshot started are
# hashed but not cached ("racy" entries, as in git's index). Kernel timestamps are
# coarse (one jiffy), so a write right after hashing can leave all stat fields
# unchanged. The margin also covers moderate clock skew between an NFS server and
# this host.
RACY_MARGIN_NS = 60 * 1_000_000_000


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


def _cache_key(info: os.stat_result) -> list[int]:
    return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _valid_cache_entry(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 5
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value[:4])
        and isinstance(value[4], str)
        and len(value[4]) == 64
        and all(character in "0123456789abcdef" for character in value[4])
    )


def load_hash_cache(path: Path) -> HashCache:
    """Read a hash cache. A missing, unreadable or malformed cache is treated as empty."""
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}

    if not isinstance(value, dict) or value.get("version") != HASH_CACHE_VERSION:
        return {}
    entries = value.get("entries")
    if not isinstance(entries, dict):
        return {}

    return {
        relative: entry
        for relative, entry in entries.items()
        if isinstance(relative, str) and _valid_cache_entry(entry)
    }


def save_hash_cache(path: Path, cache: HashCache) -> None:
    """Write the cache atomically: a crash leaves either the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            {"version": HASH_CACHE_VERSION, "entries": cache},
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)


def _walk_error(error: OSError) -> None:
    raise ExportError(f"cannot scan repository directory: {error.filename}") from error


def snapshot(
    root: Path,
    pulse: Pulse | None = None,
    cache: HashCache | None = None,
    *,
    racy_margin_ns: int = RACY_MARGIN_NS,
) -> Inventory:
    """Inventory every regular file and symlink below root.

    Without a cache every regular file is hashed. With a cache, a file whose inode,
    size, mtime and ctime match the cached entry reuses the cached SHA-256. ctime
    cannot be set from user space, so content rewritten with the old size and mtime
    restored is still detected. The cache is updated in place: entries are refreshed
    and paths that no longer exist are dropped. The inventory format is unchanged.
    """
    if root.is_symlink() or not root.is_dir():
        raise ExportError("repository directory is missing or is a symlink")

    inventory: Inventory = {}
    refreshed: HashCache = {}
    racy_cutoff_ns = time.time_ns() - racy_margin_ns

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
                key = _cache_key(before)
                cached = cache.get(relative) if cache is not None else None
                if cached is not None and cached[:4] == key:
                    digest = str(cached[4])
                else:
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
                if max(before.st_mtime_ns, before.st_ctime_ns) < racy_cutoff_ns:
                    refreshed[relative] = [*key, digest]
            else:
                raise ExportError(f"unsupported filesystem entry: {relative}")

            if pulse is not None:
                pulse(None, f"Inventoried {len(inventory)} entries")

    if cache is not None:
        cache.clear()
        cache.update(refreshed)

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
