#!/usr/bin/env python3
"""Measure inventory time with and without the hash cache on an existing mirror.

Read-only for the mirror: it only stats and reads files below --root. The hash cache
is written to --cache-dir (default: a new directory under /tmp), never to the mirror
or to MirrorHub's state volume.

Runs:
  fill    empty cache: hashes every file, same work as before the cache existed
  cached  cache from the fill run: only files with changed stat data are hashed
  baseline (optional, --baseline) no cache at all, for a direct comparison

Example:
  python3 scripts/bench_hash_cache.py --root /mnt/repository/ubuntu --drop-caches

--drop-caches empties the page cache before each run (needs root). Without it, a
mirror that fits in RAM is partly served from memory and the fill run looks faster
than it would be after a reboot.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mirrorhub.export import manifest


class Progress:
    """Throttled progress line on stderr."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.last = 0.0

    def __call__(self, _pct: float | None, text: str) -> None:
        """Print a progress line at most every five seconds."""
        now = time.monotonic()
        if now - self.last >= 5:
            self.last = now
            print(f"  [{self.label}] {text}", file=sys.stderr, flush=True)


def drop_caches() -> None:
    """Sync and drop the page cache (needs root), so reads come from disk."""
    os.sync()
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="ascii")
    except OSError as exc:
        raise SystemExit(f"--drop-caches needs root: {exc}") from exc


def measure(
    label: str,
    root: Path,
    cache: manifest.HashCache | None,
    drop: bool,
) -> tuple[manifest.Inventory, dict[str, float]]:
    """Build an inventory with or without the hash cache; return it with timings."""
    if drop:
        drop_caches()

    hashed = {"files": 0, "bytes": 0}
    original = manifest.file_hash

    def counting(path: Path, pulse: manifest.Pulse | None = None) -> str:
        hashed["files"] += 1
        hashed["bytes"] += path.lstat().st_size
        return original(path, pulse)

    manifest.file_hash = counting  # type: ignore[assignment]
    print(f"{label}: running ...", file=sys.stderr, flush=True)
    started = time.perf_counter()
    try:
        inventory = manifest.snapshot(root, Progress(label), cache)
    finally:
        manifest.file_hash = original  # type: ignore[assignment]
    seconds = time.perf_counter() - started

    return inventory, {
        "seconds": seconds,
        "entries": len(inventory),
        "data_bytes": sum(entry["size"] for entry in inventory.values()),
        "files_hashed": hashed["files"],
        "bytes_hashed": hashed["bytes"],
    }


def main() -> int:
    """Run the command line; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="one repository directory")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="also run without any cache")
    args = parser.parse_args()

    root = args.root.resolve()
    cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="mirrorhub-bench-"))
    if cache_dir.resolve().is_relative_to(root):
        raise SystemExit("--cache-dir must not be inside the mirror")
    cache_path = cache_dir / "hashcache.json"

    rows: list[tuple[str, dict[str, float]]] = []

    if args.baseline:
        baseline, stats = measure("baseline", root, None, args.drop_caches)
        rows.append(("no cache (old behaviour)", stats))
    else:
        baseline = None

    cache: manifest.HashCache = {}
    filled, stats = measure("fill", root, cache, args.drop_caches)
    rows.append(("empty cache, fills it", stats))
    manifest.save_hash_cache(cache_path, cache)

    cache = manifest.load_hash_cache(cache_path)
    cached, stats = measure("cached", root, cache, args.drop_caches)
    rows.append(("cache from previous run", stats))

    identical = filled == cached and (baseline is None or baseline == filled)

    print()
    print(f"root:        {root}")
    print(f"cache file:  {cache_path} ({cache_path.stat().st_size / 1e6:.1f} MB)")
    print(f"drop caches: {'yes' if args.drop_caches else 'no'}")
    print()
    print(
        f"{'run':<26} {'time':>10} {'entries':>10} {'data GB':>9} {'hashed':>10} {'GB hashed':>10}"
    )
    for label, row in rows:
        print(
            f"{label:<26} {row['seconds']:>9.1f}s {int(row['entries']):>10} "
            f"{row['data_bytes'] / 1e9:>9.1f} {int(row['files_hashed']):>10} "
            f"{row['bytes_hashed'] / 1e9:>10.1f}"
        )
    print()
    print(f"inventories identical: {'yes' if identical else 'NO'}")
    if not identical:
        print("A file changed between runs (for example a sync was running).")
        print("Re-run while no sync or export is active for this repository.")
    print(
        "Files changed less than 60 s before a run are never cached; "
        "a few may be hashed again in the cached run."
    )
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
