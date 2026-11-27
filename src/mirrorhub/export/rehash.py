"""Rehash a repository: rebuild its hash cache from the file contents.

Exports trust the hash cache for files whose inode, size, mtime and ctime are
unchanged, as git, restic and borg do. Content that changes without any stat
change (silent disk corruption, block-level edits on the storage) is not seen that
way. A rehash reads every regular file once, compares each digest with a cache
entry whose stat key still matches and rebuilds the cache.

A mismatch keeps the old cache entry: the next incremental export then neither
sends the changed content nor treats the file as unchanged when packing it (the
digest comparison while packing refuses it). The operator restores the file, for
example by deleting it and syncing again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mirrorhub.export import manifest
from mirrorhub.export.job import hash_cache_path
from mirrorhub.export.manifest import ExportError, HashCache
from mirrorhub.settings import Settings

Pulse = Callable[[float | None, str], None]


@dataclass
class Result:
    """Outcome of a rehash: counts, mismatched paths and duration."""

    files: int = 0
    bytes: int = 0
    # Files with a cache entry whose stat key still matched: their digest was checked.
    compared: int = 0
    # Files whose content differs from the cache although the stat key is unchanged.
    mismatched: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        """Return the one-line summary written to the job log."""
        text = (
            f"rehashed {self.files} files ({self.bytes / 2**30:.2f} GiB) in "
            f"{self.seconds:.0f} s; {self.compared} checked against the hash cache"
        )
        if self.mismatched:
            text += (
                f"; {len(self.mismatched)} changed content without a stat change "
                "(possible disk corruption)"
            )
        return text


def run(
    root: Path,
    cache_path: Path,
    pulse: Pulse,
    *,
    threads: int = 1,
    racy_margin_ns: int = manifest.RACY_MARGIN_NS,
) -> Result:
    """Hash every regular file below root and replace the hash cache at cache_path."""
    if root.is_symlink() or not root.is_dir():
        raise ExportError("repository directory is missing or is a symlink")

    started = time.monotonic()
    old = manifest.load_hash_cache(cache_path)
    racy_cutoff_ns = time.time_ns() - racy_margin_ns
    entries = manifest._entries(root)
    total = max(len(entries), 1)
    result = Result()
    refreshed: HashCache = {}
    last_pct = -1

    def task(
        item: tuple[str, Path],
    ) -> tuple[dict[str, object], tuple[list[int], bool] | None]:
        # cache=None: every regular file is read, whatever the cache says.
        return manifest._inventory_entry(item[0], item[1], None, True, racy_cutoff_ns)

    def collect(
        index: int,
        relative: str,
        outcome: tuple[dict[str, object], tuple[list[int], bool] | None],
    ) -> None:
        nonlocal last_pct
        entry, key_info = outcome
        if key_info is not None:
            key, cacheable = key_info
            digest = str(entry["sha256"])
            size = entry["size"]
            result.files += 1
            result.bytes += size if isinstance(size, int) else 0
            cached = old.get(relative)
            if cached is not None and cached[:4] == key:
                result.compared += 1
                if cached[4] != digest:
                    result.mismatched.append(relative)
                    refreshed[relative] = cached
                    key_info = None
            if key_info is not None and cacheable:
                refreshed[relative] = [*key, digest]
        pct = int(100 * (index + 1) / total)
        if pct != last_pct:
            last_pct = pct
            pulse(float(min(pct, 99)), f"Rehashed {index + 1} of {len(entries)} entries")

    if threads <= 1:
        for index, item in enumerate(entries):
            collect(index, item[0], task(item))
    else:
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="rehash") as pool:
            for start in range(0, len(entries), 2048):
                batch = entries[start : start + 2048]
                for offset, (item, outcome) in enumerate(
                    zip(batch, pool.map(task, batch), strict=True)
                ):
                    collect(start + offset, item[0], outcome)

    manifest.save_hash_cache(cache_path, refreshed)
    result.mismatched.sort()
    result.seconds = time.monotonic() - started
    return result


def repository(slug: str, settings: Settings, pulse: Pulse) -> Result:
    """Rehash the repository's tree in the data directory (job type ``rehash``)."""
    root = settings.data_dir / slug
    if root.resolve().parent != settings.data_dir.resolve():
        raise ExportError("repository path escapes the data directory")
    return run(
        root,
        hash_cache_path(settings, slug),
        pulse,
        threads=settings.inventory_threads,
    )
