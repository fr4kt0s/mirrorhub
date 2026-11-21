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
from pathlib import Path

from mirrorhub.importer.checks import SKIP_DIRECTORIES, Inventory

SHOWN_DIFFERENCES = 10


@dataclass(frozen=True)
class TreeEntry:
    kind: str  # "file", "symlink" or "other"
    size: int
    mtime_ns: int
    target: str | None


def scan_tree(root: Path) -> dict[str, TreeEntry]:
    """lstat every entry below root once; symlinked directories count as entries."""
    found: dict[str, TreeEntry] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
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


def content_differences(inventory: Inventory, root: Path) -> list[str]:
    """SHA-256 of every regular file against the inventory (reads the whole tree)."""
    found: list[str] = []
    for relative, entry in sorted(inventory.items()):
        if entry["type"] != "file":
            continue
        digest = hashlib.sha256()
        with (root / relative).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            found.append(f"sha256: {relative}")
    return found


def summary(label: str, found: list[str]) -> str:
    shown = "; ".join(found[:SHOWN_DIFFERENCES])
    more = f"; and {len(found) - SHOWN_DIFFERENCES} more" if len(found) > SHOWN_DIFFERENCES else ""
    return f"tree does not match {label}: {len(found)} differences: {shown}{more}"
