"""Adopt an export as the receiver state when the state file is lost.

The tree is compared with the export's files.json. The default quick check reads only
file system metadata (type, size, nanosecond mtime, symlink target, no extra paths),
which is meaningful because the importer sets every mtime from files.json. --hash
also compares SHA-256 and reads every file once. Nothing is transferred or changed in
the tree; on an exact match only the state file is written.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mirrorhub.importer.checks import (
    EXPORT_ID_RE,
    SKIP_DIRECTORIES,
    SLUG_RE,
    ExportMetadata,
    Inventory,
    check_signature,
    load_metadata,
)
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import ChainError, ConfigError, IoFailure, PostCheckError
from mirrorhub.importer.logs import log_event
from mirrorhub.importer.state import StateStore

SHOWN_DIFFERENCES = 10


@dataclass(frozen=True)
class TreeEntry:
    kind: str  # "file", "symlink" or "other"
    size: int
    mtime_ns: int
    target: str | None


@dataclass(frozen=True)
class Adopted:
    slug: str
    export_id: str
    entries: int
    hashed: bool


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


def _candidates(config: Config, slug: str, export_id: str | None) -> list[Path]:
    directory = config.export_dir / slug
    if export_id is not None:
        if not EXPORT_ID_RE.fullmatch(export_id):
            raise ConfigError(f"invalid export id: {export_id!r}")
        candidate = directory / export_id
        if not (candidate / ".done").is_file() or candidate.is_symlink():
            raise ConfigError(f"export not found in the inbox: {candidate}")
        return [candidate]
    if not directory.is_dir():
        raise ConfigError(f"no inbox directory for {slug!r}: {directory}")
    published = [
        child
        for child in sorted(directory.iterdir())
        if EXPORT_ID_RE.fullmatch(child.name)
        and child.is_dir()
        and not child.is_symlink()
        and (child / ".done").is_file()
    ]
    if not published:
        raise ConfigError(f"no published exports in the inbox for {slug!r}")
    return list(reversed(published))  # newest first


def _summary(export_id: str, found: list[str]) -> str:
    shown = "; ".join(found[:SHOWN_DIFFERENCES])
    more = f"; and {len(found) - SHOWN_DIFFERENCES} more" if len(found) > SHOWN_DIFFERENCES else ""
    return f"tree does not match {export_id}: {len(found)} differences: {shown}{more}"


def adopt(
    config: Config,
    slug: str,
    *,
    export_id: str | None = None,
    hash_contents: bool = False,
) -> Adopted:
    """Write the receiver state for the export the tree matches exactly.

    export_id None means: try the published exports in the inbox, newest first.
    Refused if a state already exists or an import is unfinished.
    """
    if not SLUG_RE.fullmatch(slug):
        raise ConfigError(f"invalid slug: {slug!r}")
    store = StateStore(config.state_dir, slug)
    target = config.mirror_root / slug
    try:
        with store.locked():
            state = store.load()
            if state.last_export_id is not None or state.history:
                raise ChainError(
                    f"receiver state for {slug} exists (last={state.last_export_id}); "
                    "adopt is only for a missing state file"
                )
            if (config.staging_dir / slug / "journal.json").exists():
                raise IoFailure(
                    "staging holds an unfinished import; finish it or pass "
                    "--discard-staging to the next import first"
                )
            if not target.is_dir():
                raise ConfigError(f"no mirror tree for {slug!r}: {target}")

            candidates = _candidates(config, slug, export_id)
            tree = scan_tree(target)
            chosen: ExportMetadata | None = None
            first_failure = ""
            for candidate in candidates:
                metadata = load_metadata(candidate)
                check_signature(candidate, config.keyring, config.require_signature)
                found = differences(metadata.inventory, tree)
                if not found:
                    chosen = metadata
                    break
                if not first_failure:
                    first_failure = _summary(metadata.export_id, found)
            if chosen is None:
                prefix = "" if export_id is not None else "no export in the inbox matches; "
                raise PostCheckError(prefix + first_failure)
            if hash_contents:
                found = content_differences(chosen.inventory, target)
                if found:
                    raise PostCheckError(_summary(chosen.export_id, found))

            state.last_export_id = chosen.export_id
            state.history.append(
                {
                    "export_id": chosen.export_id,
                    "status": "adopted",
                    "mode": chosen.mode,
                    "base_export_id": chosen.base_export_id,
                    "adopted_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "hash_checked": hash_contents,
                    "entries": len(chosen.inventory),
                }
            )
            store.save(state)
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc

    log_event(
        logging.INFO,
        "adopted",
        slug=slug,
        export_id=chosen.export_id,
        hashed=hash_contents,
    )
    return Adopted(slug, chosen.export_id, len(chosen.inventory), hash_contents)
