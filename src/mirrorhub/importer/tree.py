"""Compare a mirror tree with an export inventory (files.json).

The quick comparison reads only file system metadata: every path present, same
type, size, mtime (nanoseconds) and symlink target, no extra paths. It is
meaningful because the importer sets every mtime from files.json. The content
comparison also hashes every regular file.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mirrorhub.importer.checks import SKIP_DIRECTORIES, Inventory
from mirrorhub.importer.progress import Progress

SHOWN_DIFFERENCES = 10


@dataclass(frozen=True)
class TreeEntry:
    kind: str  # "file", "symlink" or "other"
    size: int
    mtime_ns: int
    target: str | None


def scan_tree(
    root: Path, progress: Progress | None = None, directories: list[Path] | None = None
) -> dict[str, TreeEntry]:
    """lstat every entry below root once; symlinked directories count as entries.

    If `directories` is given, every directory walked (root included) is appended,
    so callers can check them later without listing the tree a second time."""
    found: dict[str, TreeEntry] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        if directories is not None:
            directories.append(base)
        links = [name for name in names if (base / name).is_symlink()]
        names[:] = [n for n in names if n not in SKIP_DIRECTORIES and n not in links]
        for name in [*filenames, *links]:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                kind, target = "symlink", os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                kind, target = "file", None
            else:
                kind, target = "other", None
            relative = path.relative_to(root).as_posix()
            found[relative] = TreeEntry(kind, info.st_size, info.st_mtime_ns, target)
            if progress is not None:
                progress.advance()
    return found


def differences(inventory: Inventory, tree: dict[str, TreeEntry]) -> list[str]:
    """Quick check: what differs between the tree and the inventory, without contents."""
    found: list[str] = []
    for relative in sorted(set(tree) - set(inventory)):
        found.append(f"extra: {relative}")
    for relative, entry in sorted(inventory.items()):
        present = tree.get(relative)
        if present is None:
            found.append(f"missing: {relative}")
        elif present.kind != entry["type"]:
            found.append(f"type: {relative}")
        elif entry["type"] == "symlink" and present.target != entry.get("target"):
            found.append(f"symlink target: {relative}")
        elif entry["type"] == "file" and present.size != entry["size"]:
            found.append(f"size: {relative}")
        elif present.mtime_ns != entry["mtime_ns"]:
            found.append(f"mtime: {relative}")
    return found


def content_differences(
    inventory: Inventory,
    root: Path,
    skip: frozenset[str] = frozenset(),
    progress: Progress | None = None,
) -> list[str]:
    """SHA-256 of every regular file against the inventory (reads the whole tree).

    Paths in skip (already reported by the quick check) are not read.
    """
    found: list[str] = []
    for relative, entry in sorted(inventory.items()):
        if entry["type"] != "file" or relative in skip:
            continue
        digest = hashlib.sha256()
        with (root / relative).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                if progress is not None:
                    progress.advance(0, len(chunk))
        if progress is not None:
            progress.advance(1)
        if digest.hexdigest() != entry["sha256"]:
            found.append(f"sha256: {relative}")
    return found


def scan(
    root: Path,
    inventory: Inventory,
    slug: str,
    export_id: str,
    directories: list[Path] | None = None,
) -> dict[str, TreeEntry]:
    """scan_tree with progress lines; the inventory size is the expected total."""
    progress = Progress("tree scan", files_total=len(inventory), slug=slug, export_id=export_id)
    entries = scan_tree(root, progress, directories)
    progress.finish()
    return entries


def hash_contents(
    inventory: Inventory,
    root: Path,
    skip: frozenset[str] = frozenset(),
    *,
    slug: str,
    export_id: str,
) -> list[str]:
    """content_differences with progress lines (files and bytes to hash as totals)."""
    files = [
        entry
        for relative, entry in inventory.items()
        if entry["type"] == "file" and relative not in skip
    ]
    progress = Progress(
        "content hash",
        files_total=len(files),
        bytes_total=sum(entry["size"] for entry in files),
        slug=slug,
        export_id=export_id,
    )
    found = content_differences(inventory, root, skip, progress)
    progress.finish()
    return found


def summary(label: str, found: list[str]) -> str:
    shown = "; ".join(found[:SHOWN_DIFFERENCES])
    more = f"; and {len(found) - SHOWN_DIFFERENCES} more" if len(found) > SHOWN_DIFFERENCES else ""
    return f"tree does not match {label}: {len(found)} differences: {shown}{more}"


# One report file per kind of difference; "symlink target" is written as symlink-target.
REPORT_FILES = {
    "missing": "missing.txt",
    "extra": "extra.txt",
    "type": "type.txt",
    "size": "size.txt",
    "mtime": "mtime.txt",
    "symlink target": "symlink-target.txt",
    "sha256": "sha256.txt",
}


def _report_line(path: str) -> str:
    """The path as one line. Names with control characters (a line break in a file
    name, possible only for extra paths) are written with backslash escapes."""
    if all(ord(character) >= 32 for character in path):
        return path
    return path.encode("unicode_escape", "backslashreplace").decode("ascii")


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", errors="surrogateescape") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_report(
    directory: Path,
    slug: str,
    export_id: str,
    found: list[str],
    *,
    hashed: bool,
) -> Path:
    """Write <directory>/<slug>/<kind>.txt (one path per line, sorted) and summary.txt.

    Every file is written, empty when there is nothing of that kind, so scripts can
    rely on them and count lines. Existing files of the same names are replaced.
    """
    target = directory / slug
    target.mkdir(parents=True, exist_ok=True)
    by_kind: dict[str, list[str]] = {kind: [] for kind in REPORT_FILES}
    for line in found:
        kind, _, path = line.partition(": ")
        by_kind.setdefault(kind, []).append(path)
    for kind, name in REPORT_FILES.items():
        paths = sorted(by_kind.get(kind, []))
        _write(target / name, "".join(f"{_report_line(path)}\n" for path in paths))
    counts = "".join(
        f"{name.removesuffix('.txt')}={len(by_kind.get(kind, []))}\n"
        for kind, name in REPORT_FILES.items()
    )
    _write(
        target / "summary.txt",
        f"slug={slug}\n"
        f"export_id={export_id}\n"
        f"checked_at={datetime.now(UTC).isoformat(timespec='seconds')}\n"
        f"check={'quick+sha256' if hashed else 'quick'}\n"
        f"total={len(found)}\n" + counts,
    )
    return target
