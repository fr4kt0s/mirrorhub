"""Repository inventories and incremental diffing."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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


# relative path -> (cache key [ino, size, mtime_ns, ctime_ns], may be cached)
StatKeys = dict[str, tuple[list[int], bool]]

_BATCH = 2048


def _entries(root: Path) -> list[tuple[str, Path]]:
    """Relative path and absolute path of every non-directory entry below root.

    Uses os.scandir: the entry type comes from the directory listing (d_type), so
    finding files and symlinks costs no stat call per entry. Symlinks to directories
    are recorded as symlinks and not followed. Skipped directories: SKIP_DIRECTORIES.
    """
    found: list[tuple[str, Path]] = []
    stack: list[tuple[str, str]] = [(str(root), "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                items = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            _walk_error(error)
        subdirectories: list[tuple[str, str]] = []
        for item in items:
            relative = f"{prefix}{item.name}"
            try:
                is_directory = item.is_dir(follow_symlinks=False)
            except OSError as error:
                _walk_error(error)
            if is_directory:
                if item.name not in SKIP_DIRECTORIES:
                    subdirectories.append((item.path, f"{relative}/"))
            else:
                found.append((relative, Path(item.path)))
        stack.extend(reversed(subdirectories))
    return found


def _inventory_entry(
    relative: str,
    path: Path,
    cache: HashCache | None,
    hash_files: bool,
    racy_cutoff_ns: int,
) -> tuple[dict[str, Any], tuple[list[int], bool] | None]:
    """Inventory entry for one path, plus its cache key for regular files.

    Runs in worker threads: no progress callback here (the caller reports progress).
    The second lstat, which detects a change while the file is read, is only needed
    when the file was actually read.
    """
    validate_relative(relative)
    before = path.lstat()

    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        validate_link(relative, target)
        after = path.lstat()
        if _identity(before) != _identity(after):
            raise ExportError(f"symlink changed during inventory: {relative}")
        return {
            "type": "symlink",
            "target": target,
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "sha256": hashlib.sha256(os.fsencode(target)).hexdigest(),
        }, None

    if not stat.S_ISREG(before.st_mode):
        raise ExportError(f"unsupported filesystem entry: {relative}")

    key = _cache_key(before)
    cached = cache.get(relative) if cache is not None else None
    if cached is not None and cached[:4] == key:
        digest = str(cached[4])
    elif hash_files:
        digest = file_hash(path)
        after = path.lstat()
        if _identity(before) != _identity(after):
            raise ExportError(f"file changed during inventory: {relative}")
    else:
        # Filled in by the caller (a full export hashes while packing).
        digest = ""
    cacheable = max(before.st_mtime_ns, before.st_ctime_ns) < racy_cutoff_ns
    return {
        "type": "file",
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": digest,
    }, (key, cacheable)


def snapshot(
    root: Path,
    pulse: Pulse | None = None,
    cache: HashCache | None = None,
    *,
    racy_margin_ns: int = RACY_MARGIN_NS,
    hash_files: bool = True,
    threads: int = 1,
    stat_keys: StatKeys | None = None,
) -> Inventory:
    """Inventory every regular file and symlink below root.

    Without a cache every regular file is hashed. With a cache, a file whose inode,
    size, mtime and ctime match the cached entry reuses the cached SHA-256. ctime
    cannot be set from user space, so content rewritten with the old size and mtime
    restored is still detected. The cache is updated in place: entries are refreshed
    and paths that no longer exist are dropped. The inventory format is unchanged.

    ``hash_files=False`` leaves ``sha256`` empty for files without a cache hit (a full
    export fills it while packing, so the data is read once). ``stat_keys`` receives
    each regular file's cache key and whether it may be cached, so the caller can
    update the cache with those digests. ``threads`` > 1 stats and hashes files in
    that many threads (setting ``inventory_threads``): on NFS each stat and open is a
    network round trip, and SHA-256 releases the GIL for large buffers.
    """
    if root.is_symlink() or not root.is_dir():
        raise ExportError("repository directory is missing or is a symlink")

    racy_cutoff_ns = time.time_ns() - racy_margin_ns
    entries = _entries(root)
    inventory: Inventory = {}
    refreshed: HashCache = {}

    def collect(
        relative: str, result: tuple[dict[str, Any], tuple[list[int], bool] | None]
    ) -> None:
        entry, key_info = result
        inventory[relative] = entry
        if key_info is not None:
            key, cacheable = key_info
            if stat_keys is not None:
                stat_keys[relative] = key_info
            if cacheable and entry["sha256"]:
                refreshed[relative] = [*key, entry["sha256"]]
        if pulse is not None:
            pulse(None, f"Inventoried {len(inventory)} of {len(entries)} entries")

    def task(item: tuple[str, Path]) -> tuple[dict[str, Any], tuple[list[int], bool] | None]:
        return _inventory_entry(item[0], item[1], cache, hash_files, racy_cutoff_ns)

    if threads <= 1:
        for item in entries:
            collect(item[0], task(item))
    else:
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="inventory") as pool:
            for start in range(0, len(entries), _BATCH):
                batch = entries[start : start + _BATCH]
                for item, result in zip(batch, pool.map(task, batch), strict=True):
                    collect(item[0], result)

    if cache is not None:
        cache.clear()
        cache.update(refreshed)

    return dict(sorted(inventory.items()))


def _hash_unchanged(path: Path, relative: str, key: list[int]) -> str:
    digest = file_hash(path)
    if _cache_key(path.lstat()) != key:
        raise ExportError(f"file changed during inventory: {relative}")
    return digest


def resolve_against_base(
    root: Path,
    current: Inventory,
    previous: Inventory,
    stat_keys: StatKeys,
    cache: HashCache,
    *,
    threads: int = 1,
) -> list[str]:
    """Hash the files whose digest the inventory left empty but whose size and mtime
    equal the base entry: only their content decides whether they changed.

    An incremental export builds its inventory with ``hash_files=False``. A file
    without a cache hit whose size or mtime differs from the base is changed anyway;
    it is hashed while packing, so its data is read once. The rest (new inode or
    ctime with the old size and mtime, or a racy entry of the base) is hashed here.
    Digests go into the inventory and, if cacheable, into the cache. Returns the
    hashed paths.
    """
    pending: list[str] = []
    for relative, entry in current.items():
        if entry["type"] != "file" or entry["sha256"]:
            continue
        base = previous.get(relative)
        if (
            base is not None
            and base.get("type") == "file"
            and base.get("size") == entry["size"]
            and base.get("mtime_ns") == entry["mtime_ns"]
        ):
            pending.append(relative)

    def task(relative: str) -> str:
        return _hash_unchanged(root / relative, relative, stat_keys[relative][0])

    if threads <= 1 or len(pending) < 2:
        digests = [task(relative) for relative in pending]
    else:
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="inventory") as pool:
            digests = list(pool.map(task, pending))

    for relative, digest in zip(pending, digests, strict=True):
        current[relative]["sha256"] = digest
        key, cacheable = stat_keys[relative]
        if cacheable:
            cache[relative] = [*key, digest]
    return pending


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
