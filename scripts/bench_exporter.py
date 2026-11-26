#!/usr/bin/env python3
"""Measure the exporter's inventory and packing on an existing mirror.

Read-only for the mirror. Modes:

  inventory  Walk the tree like an incremental export: fill a hash cache once (hashes
             every file; skip with --cache FILE from an earlier run), then time the
             cached walk with each --workers value. On NFS this is dominated by one
             stat round trip per file, so more workers help.
  full       Like a full export: inventory without hashing, then write the archive
             (hashing while packing) into --out. Writes as much as the tree holds;
             use --limit-mb to stop after that much payload.

Bytes read and written come from /proc/self/io (rchar/wchar), so they include data
served from the page cache. --drop-caches (root) empties it first.

Examples:
  python3 scripts/bench_exporter.py inventory --root /mnt/repository/ubuntu --workers 1 8 16
  python3 scripts/bench_exporter.py full --root /mnt/repository/ubuntu --out /mnt/scratch/bench \\
      --limit-mb 20000
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mirrorhub.export import manifest, pack


def _io() -> tuple[int, int]:
    fields = dict(line.split(": ", 1) for line in Path("/proc/self/io").read_text().splitlines())
    return int(fields["rchar"]), int(fields["wchar"])


def _drop_caches() -> None:
    os.sync()
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="ascii")
    except OSError as exc:
        raise SystemExit(f"--drop-caches needs root: {exc}") from exc


def _progress(label: str) -> manifest.Pulse:
    last = [0.0]

    def pulse(_pct: float | None, text: str) -> None:
        now = time.monotonic()
        if now - last[0] >= 5:
            last[0] = now
            print(f"  [{label}] {text}", file=sys.stderr, flush=True)

    return pulse


def _mib(value: int) -> str:
    return f"{value / 2**20:,.0f} MiB"


def inventory(args: argparse.Namespace) -> None:
    cache: manifest.HashCache = {}
    if args.cache and Path(args.cache).exists():
        cache = manifest.load_hash_cache(Path(args.cache))
        print(f"cache: {len(cache)} entries from {args.cache}")
    else:
        print("filling the hash cache (reads every file once) …")
        start = time.monotonic()
        manifest.snapshot(args.root, _progress("fill"), cache, workers=max(args.workers))
        print(f"  filled: {len(cache)} files in {time.monotonic() - start:.1f}s")
        if args.cache:
            manifest.save_hash_cache(Path(args.cache), cache)
    for workers in args.workers:
        if args.drop_caches:
            _drop_caches()
        reads, _writes = _io()
        start = time.monotonic()
        entries = manifest.snapshot(args.root, _progress(f"w{workers}"), cache, workers=workers)
        elapsed = time.monotonic() - start
        read_after, _ = _io()
        print(
            f"cached inventory: {len(entries)} entries, workers={workers:2d}: "
            f"{elapsed:7.1f}s, read {_mib(read_after - reads)}"
        )


def full(args: argparse.Namespace) -> None:
    out: Path = args.out
    if out.resolve().is_relative_to(args.root.resolve()):
        raise SystemExit("--out must not be inside --root")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    if args.drop_caches:
        _drop_caches()

    reads, writes = _io()
    start = time.monotonic()
    current = manifest.snapshot(
        args.root, _progress("inventory"), {}, hash_files=False, workers=args.workers[0]
    )
    inventory_s = time.monotonic() - start
    changed = sorted(current)
    if args.limit_mb:
        budget, chosen = args.limit_mb * 2**20, []
        for path in changed:
            chosen.append(path)
            budget -= current[path]["size"]
            if budget <= 0:
                break
        changed = chosen
    payload = sum(current[path]["size"] for path in changed)
    pack_start = time.monotonic()
    parts, _digests = pack.write_archive(
        args.root,
        out,
        current,
        changed,
        _progress("pack"),
        compress=args.compress,
        part_bytes=args.split_mb * 2**20,
    )
    pack_s = time.monotonic() - pack_start
    read_after, write_after = _io()
    rate = payload / 2**20 / max(pack_s, 1e-9)
    factor = (read_after - reads) / max(payload, 1)
    print(
        f"inventory {len(current)} entries in {inventory_s:.1f}s; packed {len(changed)} "
        f"entries ({_mib(payload)}) in {pack_s:.1f}s = {rate:.0f} MiB/s"
    )
    print(
        f"read {_mib(read_after - reads)} ({factor:.2f} x payload), "
        f"written {_mib(write_after - writes)}, {len(parts)} part(s) in {out}"
    )
    if not args.keep:
        shutil.rmtree(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=["inventory", "full"])
    parser.add_argument("--root", type=Path, required=True, help="repository directory")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 8])
    parser.add_argument("--cache", help="hash cache file to reuse/write (inventory mode)")
    parser.add_argument("--out", type=Path, help="empty scratch directory (full mode)")
    parser.add_argument("--limit-mb", type=int, default=0, help="stop packing after this much")
    parser.add_argument("--split-mb", type=int, default=0)
    parser.add_argument("--compress", action="store_true", help="tar.zst instead of tar")
    parser.add_argument("--keep", action="store_true", help="keep the archive in --out")
    parser.add_argument("--drop-caches", action="store_true")
    args = parser.parse_args()
    if args.mode == "full" and args.out is None:
        parser.error("full needs --out")
    (inventory if args.mode == "inventory" else full)(args)


if __name__ == "__main__":
    main()
