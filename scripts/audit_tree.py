#!/usr/bin/env python3
"""Audit MirrorHub inventories and APT indexes; write dated reports only on errors.

Usage:
    python3 scripts/audit_tree.py EXPORT_DIR TREE_DIR [--apt | --apt-hash]
    python3 scripts/audit_tree.py TREE_DIR --apt-only [--apt-hash]
    python3 scripts/audit_tree.py MIRROR_ROOT --all [--apt-hash]

--all (alias -all) discovers direct child directories containing dists/.
It implies APT-only checking and continues after a repository failure.

Example:
    python3 scripts/audit_tree.py /srv/mirrorhub/mirror --all --log-dir ./audit-logs

Clean audits create no report or log directory. Errors produce a UTC-dated file:
    ansible-missing-20261108T185900123456Z.log
Missing paths are sorted and deduplicated, WITH slug prefix:
    ansible/pool/main/r/resolvelib/example.deb
Other errors are comment lines beginning '#'. Fatal errors may leave the audit
incomplete; see the comment and exit code. Existing reports are never overwritten
or removed. --log-dir defaults to the current working directory and must be outside
the checked mirror/export trees (outside the whole mirror root with --all).

The mirror and export are never modified. Filename lookups remain relative to each
slug tree; only report paths receive the slug prefix. All terminal diagnostics show
the slug. Use trusted inventory metadata and stop writers during auditing.

APT: read one Packages index per directory, preferring .gz, then .xz, then plain.
Check existence and declared size; --apt-hash checks SHA256 when provided. Identical
references are checked once; conflicting declarations separately. Sources indexes,
Release signatures, Arch databases and upstream availability are not checked.
Inventory: compare type, size, hash and symlink target, not mtimes or permissions.
Use files.json from the last applied export, including when it is incremental.
Skip .temp, .tmp, .mirrorhub, .rsync-partial and .mirrorhub-import directories.

Exit codes: 0 clean, 1 audit findings, 2 input/I/O error. --all returns the highest
per-slug code; no APT repositories found is exit 2. This is not a repair tool.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import os
import sys
import tempfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

SKIP = {".temp", ".tmp", ".mirrorhub", ".rsync-partial", ".mirrorhub-import"}


@dataclass
class Findings:
    slug: str
    missing: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    count: int = 0

    def add(self, kind: str, relative: str, detail: str = "") -> None:
        self.count += 1
        message = f"{kind} {relative!r}" + (f" ({detail})" if detail else "")
        print(f"[{self.slug}] {message}")
        if kind in {"MISSING", "APT-MISSING"}:
            self.missing.add(relative)
        else:
            self.errors.append(message)


def safe_relative(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def contained(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def differs(path: Path, entry: Mapping[str, Any]) -> bool:
    if entry["type"] == "symlink":
        return not (path.is_symlink() and os.readlink(path) == entry["target"])
    if path.is_symlink() or not path.is_file():
        return True
    return path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]


def walk_error(error: OSError) -> None:
    raise error


def check_inventory(export: Path, tree: Path, findings: Findings) -> None:
    inventory = json.loads((export / "files.json").read_text(encoding="utf-8"))
    if not isinstance(inventory, dict):
        raise ValueError("files.json must be an object")
    for relative, entry in sorted(inventory.items()):
        if not safe_relative(relative) or not isinstance(entry, dict):
            raise ValueError(f"invalid inventory entry: {relative!r}")
        path = tree / relative
        if not contained(path.parent, tree):
            findings.add("UNSAFE", relative)
        elif not os.path.lexists(path):
            findings.add("MISSING", relative)
        elif entry["type"] != "symlink" and not contained(path, tree):
            findings.add("UNSAFE", relative)
        elif differs(path, entry):
            findings.add("DIFFERS", relative)
    present: set[str] = set()
    for directory, dirs, files in os.walk(tree, onerror=walk_error):
        base = Path(directory)
        links = [name for name in dirs if (base / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in SKIP and name not in links]
        for name in [*files, *links]:
            present.add((base / name).relative_to(tree).as_posix())
    for relative in sorted(present - set(inventory)):
        findings.add("EXTRA", relative)


def indexes(tree: Path) -> list[Path]:
    found: list[Path] = []
    for directory, dirs, files in os.walk(tree / "dists", onerror=walk_error):
        dirs[:] = [name for name in dirs if name not in SKIP]
        for name in ("Packages.gz", "Packages.xz", "Packages"):
            if name in files:
                found.append(Path(directory) / name)
                break
    return sorted(found)


def open_index(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    if path.suffix == ".xz":
        return lzma.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def packages(path: Path) -> Iterator[dict[str, str]]:
    entry: dict[str, str] = {}
    with open_index(path) as handle:
        for line in handle:
            if not line.strip():
                if entry:
                    yield entry
                    entry = {}
            elif not line.startswith((" ", "\t")):
                key, separator, value = line.partition(":")
                if separator and key in {"Package", "Filename", "Size", "SHA256"}:
                    entry[key] = value.strip()
    if entry:
        yield entry


def apt_problem(tree: Path, entry: Mapping[str, str], hashes: bool) -> str | None:
    name = entry.get("Filename", "")
    if not safe_relative(name) or not contained(tree / name, tree):
        return "BADNAME"
    path = tree / name
    if not path.exists():
        return "MISSING"
    if not path.is_file():
        return "TYPE"
    size = entry.get("Size", "")
    if size and (not size.isascii() or not size.isdigit()):
        return "METADATA"
    if size and path.stat().st_size != int(size):
        return "SIZE"
    expected = entry.get("SHA256", "").lower()
    if hashes and expected and digest(path) != expected:
        return "HASH"
    return None


def check_apt(tree: Path, hashes: bool, findings: Findings) -> None:
    found = indexes(tree)
    if not found:
        findings.add("APT-INDEX", "dists/", "no Packages index found")
        return
    seen: set[tuple[str, str, str]] = set()
    for index in found:
        label = index.relative_to(tree).as_posix()
        listed = 0
        before = findings.count
        try:
            if not contained(index, tree):
                raise ValueError("index points outside mirror")
            for entry in packages(index):
                listed += 1
                name = entry.get("Filename", "")
                identity = (name, entry.get("Size", ""), entry.get("SHA256", ""))
                if identity in seen:
                    continue
                seen.add(identity)
                kind = apt_problem(tree, entry, hashes)
                if kind:
                    findings.add(f"APT-{kind}", name, f"listed in {label}")
        except (OSError, EOFError, ValueError, lzma.LZMAError, zlib.error) as exc:
            findings.add("APT-INDEX", label, f"cannot check: {exc}")
        print(
            f"[{tree.name}] APT {label}: {listed} packages, "
            f"{findings.count - before} problem(s)"
        )


def write_report(log_dir: Path, findings: Findings) -> Path:
    """Publish a uniquely named, dated report without replacing existing files."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=log_dir, delete=False
        ) as handle:
            temporary = Path(handle.name)
            for name in sorted(findings.missing):
                handle.write(f"{findings.slug}/{name}\n")
            for error in findings.errors:
                for line in error.splitlines():
                    handle.write(f"# {line}\n")
            handle.flush()
            os.fsync(handle.fileno())
        report = log_dir / f"{findings.slug}-missing-{stamp}.log"
        suffix = 0
        while True:
            try:
                os.link(temporary, report)
                return report
            except FileExistsError:
                suffix += 1
                report = log_dir / f"{findings.slug}-missing-{stamp}-{suffix}.log"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def audit(tree: Path, export: Path | None, apt: bool, hashes: bool, log_dir: Path) -> int:
    findings = Findings(tree.name)
    print(f"[{tree.name}] starting audit: {tree}")
    try:
        if not safe_relative(tree.name):
            raise ValueError("invalid repository directory name")
        if contained(log_dir, tree) or (export is not None and contained(log_dir, export)):
            raise ValueError("--log-dir must be outside mirror/export directories")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    code = 0
    try:
        if not tree.is_dir():
            raise ValueError(f"mirror directory not found: {tree}")
        if export is not None:
            check_inventory(export, tree, findings)
        if apt or hashes:
            check_apt(tree, hashes, findings)
        code = 1 if findings.count else 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        findings.add("ERROR", str(tree), f"audit incomplete: {exc}")
        code = 2
    if findings.count:
        try:
            report = write_report(log_dir, findings)
            print(f"[{tree.name}] error report: {report}")
        except OSError as exc:
            print(f"[{tree.name}] cannot write error report: {exc}", file=sys.stderr)
            code = 2
    else:
        print(f"[{tree.name}] clean; no log file created")
    print(f"[{tree.name}] {findings.count} problem(s)")
    return code


def audit_all(root: Path, hashes: bool, log_dir: Path) -> int:
    try:
        if not root.is_dir():
            raise ValueError(f"mirror root not found: {root}")
        if contained(log_dir, root):
            raise ValueError("--log-dir must be outside the entire mirror root with --all")
        trees = []
        for child in sorted(root.iterdir()):
            if child.name in SKIP or child.is_symlink() or not child.is_dir():
                continue
            if (child / "dists").is_dir():
                trees.append(child)
            else:
                print(f"[{child.name}] skipped: no dists/ directory")
        if not trees:
            raise ValueError(f"no APT repositories found directly below {root}")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    results = [audit(tree, None, True, hashes, log_dir) for tree in trees]
    counts = {code: results.count(code) for code in (0, 1, 2)}
    print(
        f"ALL: {len(trees)} repositories, {counts[0]} clean, "
        f"{counts[1]} with problems, {counts[2]} failed"
    )
    return max(results)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="+", metavar="DIR")
    parser.add_argument("--apt", action="store_true", help="check APT files and sizes")
    parser.add_argument("--apt-hash", action="store_true", help="also check package SHA256")
    parser.add_argument("--apt-only", action="store_true", help="only TREE_DIR, no files.json")
    parser.add_argument("--all", "-all", dest="all_slugs", action="store_true")
    parser.add_argument(
        "--log-dir", type=Path, default=Path.cwd(), help="dated error-report folder"
    )
    args = parser.parse_args(argv)
    only = args.apt_only or args.all_slugs
    if len(args.paths) != (1 if only else 2):
        parser.error(
            "give one root/tree with --all/--apt-only; otherwise export and tree"
        )
    tree = Path(args.paths[-1]).absolute()
    if args.all_slugs:
        return audit_all(tree, args.apt_hash, args.log_dir)
    export = None if only else Path(args.paths[0]).absolute()
    return audit(tree, export, args.apt or only, args.apt_hash, args.log_dir)


if __name__ == "__main__":
    sys.exit(main())
