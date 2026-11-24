#!/usr/bin/env python3
"""Measure the receiver importer on synthetic exports, optionally for several versions.

Builds exports in the export format (data.tar, files.json, manifest.json, deleted.txt,
MANIFEST.sha256, .done) below --work and imports them with the importer CLI, each run
in its own process and into empty mirror/state directories. Nothing outside --work is
written.

Scenarios:
  small  full export of many small files (default 30 000 x 2 KiB): per-file costs
  large  full export of fewer large files (default 400 x 2.5 MiB = 1 GiB): data passes
  chain  full export of the small tree, imported first (not timed), then N incremental
         exports changing 1 % of the files each, imported in one `import --slug` run:
         per-export tree walks

Each run reports wall time, fsync calls, and bytes read/written through system calls
(rchar/wchar from /proc/self/io; includes reads served from the page cache).

Compare two versions of the importer by pointing --src at their src directories:

  git worktree add /tmp/importer-before 7a3f3b2
  python3 scripts/bench_importer.py --work /tmp/bench \\
      --src before=/tmp/importer-before/src --src after=src

--drop-caches empties the page cache before each timed run (needs root). The disk
matters: an fsync costs about 0.5 ms on an SSD and 5-20 ms on a hard disk or USB
medium, so per-file flushes weigh far more on the receiver than in a VM.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE_MTIME = 1_700_000_000_000_000_000
STAMPS = [f"202611{day:02d}T000000Z" for day in range(1, 29)]

Entry = tuple[int, int]  # size, generation (content and mtime depend on it)


def _content(path: str, size: int, generation: int) -> bytes:
    block = hashlib.sha256(f"{path}:{generation}".encode()).digest()
    return (block * (size // len(block) + 1))[:size]


def _mtime(generation: int) -> int:
    return BASE_MTIME + generation * 1_000_000_000


def _inventory(tree: dict[str, Entry]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, (size, generation) in sorted(tree.items()):
        result[path] = {
            "type": "file",
            "size": size,
            "mtime_ns": _mtime(generation),
            "sha256": hashlib.sha256(_content(path, size, generation)).hexdigest(),
        }
    return result


def build_export(
    inbox: Path,
    slug: str,
    export_id: str,
    tree: dict[str, Entry],
    payload: list[str],
    deleted: list[str],
    base: str | None,
) -> None:
    directory = inbox / slug / export_id
    directory.mkdir(parents=True)
    mode = export_id.rsplit("-", 1)[1]
    with tarfile.open(directory / "data.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        for path in payload:
            size, generation = tree[path]
            info = tarfile.TarInfo(path)
            info.size = size
            info.mode = 0o644
            info.mtime = _mtime(generation) // 1_000_000_000
            tar.addfile(info, io.BytesIO(_content(path, size, generation)))
    inventory = _inventory(tree)
    (directory / "files.json").write_text(json.dumps(inventory, sort_keys=True))
    (directory / "deleted.txt").write_text("".join(f"{path}\n" for path in deleted))
    manifest = {
        "format_version": 1,
        "mode": mode,
        "export_id": export_id,
        "repo": slug,
        "base_export_id": base,
        "created_at": "2026-11-24T00:00:00Z",
        "archive_format": "tar",
        "archive_parts": ["data.tar"],
        "payload_paths": payload,
        "counts": {"inventory": len(inventory), "payload": len(payload), "deleted": len(deleted)},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    lines = []
    for name in sorted(item.name for item in directory.iterdir()):
        digest = hashlib.sha256()
        with (directory / name).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {name}\n")
    (directory / "MANIFEST.sha256").write_text("".join(lines))
    (directory / ".done").write_text(export_id)


def _tree(count: int, size: int, prefix: str = "p") -> dict[str, Entry]:
    tree = {f"pool/main/{i % 97:02d}/{i % 13}/{prefix}{i}.deb": (size, 0) for i in range(count)}
    tree["dists/stable/Release"] = (4096, 0)
    return tree


def prepare(work: Path, args: argparse.Namespace) -> None:
    """Build the inboxes once; later runs reuse them (marker file with the settings)."""
    settings = json.dumps(
        [args.files, args.size, args.large_files, args.large_size, args.incrementals]
    )
    marker = work / "exports.json"
    if marker.is_file() and marker.read_text() == settings:
        return
    shutil.rmtree(work / "inbox", ignore_errors=True)
    started = time.monotonic()

    small = _tree(args.files, args.size)
    build_export(work / "inbox", "small", f"{STAMPS[0]}-full", small, sorted(small), [], None)
    large = _tree(args.large_files, args.large_size)
    build_export(work / "inbox", "large", f"{STAMPS[0]}-full", large, sorted(large), [], None)

    chain = dict(small)
    previous = f"{STAMPS[0]}-full"
    build_export(work / "inbox", "chain", previous, chain, sorted(chain), [], None)
    paths = sorted(chain)
    step = max(1, len(paths) // max(1, len(paths) // 100))
    for number in range(1, args.incrementals + 1):
        changed = paths[number % step :: step]
        for path in changed:
            chain[path] = (chain[path][0], number)
        export_id = f"{STAMPS[number]}-incr"
        build_export(work / "inbox", "chain", export_id, chain, sorted(changed), [], previous)
        previous = export_id
    marker.write_text(settings)
    print(f"exports built in {time.monotonic() - started:.1f} s", file=sys.stderr)


def _io_counters() -> dict[str, int]:
    try:
        text = Path("/proc/self/io").read_text()
    except OSError:
        return {}
    return {key: int(value) for key, value in (line.split(": ") for line in text.splitlines())}


def child(arguments: list[str]) -> None:
    """Run one import in this process (PYTHONPATH selects the importer version)."""
    work, scenario, label = Path(arguments[0]), arguments[1], arguments[2]
    calls = {"fsync": 0}
    real_fsync = os.fsync

    def counting_fsync(descriptor: int) -> None:
        calls["fsync"] += 1
        real_fsync(descriptor)

    os.fsync = counting_fsync  # type: ignore[assignment]

    from mirrorhub.importer.cli import main

    run = work / "runs" / label / scenario
    shutil.rmtree(run, ignore_errors=True)
    run.mkdir(parents=True)
    inbox = run / "inbox"
    inbox.mkdir()
    exports = sorted((work / "inbox" / scenario).iterdir())
    config = run / "importer.toml"
    config.write_text(
        f'export_dir = "{inbox}"\nmirror_root = "{run / "mirror"}"\nstate_dir = "{run / "state"}"\n'
    )
    command = ["--config", str(config), "--log-level", "error", "import", "--slug", scenario]

    def link(paths: list[Path]) -> None:
        """Hard-link exports into the run's inbox (the importer skips symlinked ones)."""
        for path in paths:
            shutil.copytree(path, inbox / scenario / path.name, copy_function=os.link)

    sink = io.StringIO()
    if scenario == "chain":  # the full export is setup, not measured
        link(exports[:1])
        with redirect_stdout(sink):
            if main(command) != 0:
                raise SystemExit(f"setup import failed: {sink.getvalue()}")
        exports = exports[1:]
    link(exports)
    _drop_caches_if_requested()
    calls["fsync"] = 0
    before = _io_counters()
    started = time.monotonic()
    with redirect_stdout(sink):
        code = main(command)
    elapsed = time.monotonic() - started
    after = _io_counters()
    if code != 0:
        raise SystemExit(f"import failed with exit code {code}: {sink.getvalue()}")
    result = {
        "label": label,
        "scenario": scenario,
        "seconds": round(elapsed, 2),
        "fsync": calls["fsync"],
        "read_mib": round((after.get("rchar", 0) - before.get("rchar", 0)) / 2**20),
        "written_mib": round((after.get("wchar", 0) - before.get("wchar", 0)) / 2**20),
    }
    print(json.dumps(result))
    shutil.rmtree(run, ignore_errors=True)


def _drop_caches_if_requested() -> None:
    if os.environ.get("BENCH_DROP_CACHES") != "1":
        return
    os.sync()
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n")
    except OSError as exc:
        print(f"cannot drop caches: {exc}", file=sys.stderr)


def _table(results: list[dict[str, Any]]) -> str:
    columns: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("scenario", lambda r: r["scenario"]),
        ("version", lambda r: r["label"]),
        ("time (s)", lambda r: f"{r['seconds']:.2f}"),
        ("fsync calls", lambda r: str(r["fsync"])),
        ("read (MiB)", lambda r: str(r["read_mib"])),
        ("written (MiB)", lambda r: str(r["written_mib"])),
    ]
    rows = [[name for name, _ in columns]] + [[cell(r) for _, cell in columns] for r in results]
    widths = [max(len(row[i]) for row in rows) for i in range(len(columns))]
    lines = [
        "| " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) + " |"
        for row in rows
    ]
    lines.insert(1, "|" + "|".join("-" * (w + 2) for w in widths) + "|")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2:])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True, help="scratch directory")
    parser.add_argument(
        "--src",
        action="append",
        metavar="LABEL=DIR",
        help="importer version to measure (its src directory); repeatable. "
        "Default: current=<this checkout>/src",
    )
    parser.add_argument("--scenario", action="append", choices=["small", "large", "chain"])
    parser.add_argument("--files", type=int, default=30_000)
    parser.add_argument("--size", type=int, default=2048)
    parser.add_argument("--large-files", type=int, default=400)
    parser.add_argument("--large-size", type=int, default=2_621_440)
    parser.add_argument("--incrementals", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1, help="runs per version and scenario")
    parser.add_argument("--drop-caches", action="store_true", help="needs root")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    args = parser.parse_args()

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    prepare(work, args)
    versions = [item.split("=", 1) for item in (args.src or [f"current={ROOT / 'src'}"])]
    results: list[dict[str, Any]] = []
    environment = dict(os.environ)
    environment["BENCH_DROP_CACHES"] = "1" if args.drop_caches else "0"
    for scenario in args.scenario or ["small", "large", "chain"]:
        for _ in range(args.repeat):
            for label, source in versions:
                environment["PYTHONPATH"] = str(Path(source).resolve())
                output = subprocess.run(  # noqa: S603
                    [sys.executable, __file__, "--child", str(work), scenario, label],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if output.returncode != 0:
                    print(output.stderr, file=sys.stderr)
                    return 1
                result = json.loads(output.stdout.strip().splitlines()[-1])
                print(json.dumps(result), file=sys.stderr)
                results.append(result)
    print(_table(results))
    if args.json is not None:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
