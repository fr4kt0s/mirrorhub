"""Copies of export metadata on the local state volume.

An external pickup tool may move published exports out of the drop folder. The
next incremental export only needs the previous export's inventory, so MirrorHub
keeps the small metadata files of every export under
``<state>/export-meta/<slug>/<export_id>/`` and reads the base from there. The
archive parts of the previous export are neither needed nor hashed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mirrorhub.export import manifest
from mirrorhub.export.manifest import ExportError, Inventory
from mirrorhub.security.validators import validate_slug
from mirrorhub.settings import Settings

META_FILES = (
    "files.json",
    "manifest.json",
    "MANIFEST.sha256",
    "MANIFEST.sha256.sig",
    "deleted.txt",
)
REQUIRED_FILES = ("files.json", "manifest.json", "MANIFEST.sha256")
# Written into a copy after the export's .done marker. A copy without it may belong to an
# export that never got published (failure between storing the copy and .done).
PUBLISHED_MARKER = ".published"
EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-(?:full|incr)$")
DEFAULT_KEEP = 30
STALE_TMP_S = 3600


def _checked(slug: str, export_id: str) -> None:
    if validate_slug(slug) is not None:
        raise ExportError(f"invalid repository slug: {slug!r}")
    if not EXPORT_ID_RE.fullmatch(export_id):
        raise ExportError(f"invalid export id: {export_id!r}")


def root(settings: Settings) -> Path:
    return settings.state_dir / "export-meta"


def directory(settings: Settings, slug: str, export_id: str) -> Path:
    _checked(slug, export_id)
    return root(settings) / slug / export_id


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(settings: Settings, slug: str, export_id: str, source: Path) -> Path:
    """Copy the metadata files of an export from source; atomic per export."""
    target = directory(settings, slug, export_id)
    for name in REQUIRED_FILES:
        if not (source / name).is_file():
            raise ExportError(f"cannot store export metadata: {name} is missing")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{export_id}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(mode=0o750)

    for name in META_FILES:
        path = source / name
        if path.is_file() and not path.is_symlink():
            shutil.copyfile(path, temporary / name)
            with (temporary / name).open("rb") as handle:
                os.fsync(handle.fileno())
    _fsync_directory(temporary)

    if target.exists():
        shutil.rmtree(target)
    os.rename(temporary, target)
    _fsync_directory(target.parent)
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            entries[parts[1].strip()] = parts[0].lower()
    return entries


def read_base(location: Path, slug: str, export_id: str) -> Inventory:
    """Load and check the inventory of a base export from an export or metadata directory.

    Only what the next export relies on is checked: files.json and manifest.json
    match their entries in MANIFEST.sha256, the identity matches, and the inventory
    count matches the manifest. Archive parts are not required.
    """
    for name in REQUIRED_FILES:
        if not (location / name).is_file():
            raise ExportError(f"base export {export_id}: {name} is missing")

    checksums = _checksums(location / "MANIFEST.sha256")
    for name in ("files.json", "manifest.json"):
        expected = checksums.get(name)
        if expected is None:
            raise ExportError(f"base export {export_id}: {name} is not in MANIFEST.sha256")
        if _sha256(location / name) != expected:
            raise ExportError(f"base export {export_id}: checksum mismatch for {name}")

    try:
        metadata: dict[str, Any] = json.loads((location / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"base export {export_id}: manifest.json is invalid: {exc}") from exc
    if metadata.get("repo") != slug or metadata.get("export_id") != export_id:
        raise ExportError("base export identity does not match repository")

    inventory = manifest.load(location / "files.json")
    if len(inventory) != metadata.get("counts", {}).get("inventory"):
        raise ExportError(f"base export {export_id}: inventory count does not match manifest")
    return inventory


def load_base(settings: Settings, slug: str, export_id: str) -> Inventory:
    """Inventory of the previous export: metadata copy first, drop folder as fallback.

    The fallback covers exports published before metadata copies existed; a copy is
    created on the way so the drop folder is not needed again.
    """
    stored = directory(settings, slug, export_id)
    if stored.is_dir():
        return read_base(stored, slug, export_id)

    published = settings.export_dir / slug / export_id
    if (published / ".done").is_file():
        inventory = read_base(published, slug, export_id)
        save(settings, slug, export_id, published)
        return inventory

    raise ExportError(
        f"base export {export_id} not found: no metadata copy and the export directory "
        "was moved or deleted; run a full export"
    )


@dataclass(frozen=True)
class PublishedCopy:
    """What the marker of a published copy records; enough to pick a base."""

    export_id: str
    created_at: str
    mode: str
    base_export_id: str | None


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def mark_published(
    settings: Settings,
    slug: str,
    export_id: str,
    *,
    metadata: dict[str, Any] | None = None,
    job_id: int | None = None,
) -> Path:
    """Record in the copy that the export was published (idempotent).

    metadata is the export's manifest.json; read from the copy when not given.
    """
    target = directory(settings, slug, export_id)
    marker = target / PUBLISHED_MARKER
    if marker.is_file():
        return marker
    if not target.is_dir():
        raise ExportError(f"no metadata copy of {export_id} to mark as published")
    if metadata is None:
        try:
            metadata = json.loads((target / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ExportError(f"{export_id}: manifest.json is unreadable: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("export_id") != export_id:
        raise ExportError(f"{export_id}: manifest.json does not describe this export")
    _write_atomic(
        marker,
        {
            "export_id": export_id,
            "created_at": str(metadata.get("created_at") or ""),
            "mode": str(metadata.get("mode") or ""),
            "base_export_id": metadata.get("base_export_id"),
            "published_at": datetime.now(UTC).isoformat(),
            "job_id": job_id,
        },
    )
    return marker


def _read_marker(path: Path) -> PublishedCopy | None:
    try:
        raw = json.loads((path / PUBLISHED_MARKER).read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("export_id") != path.name:
        return None
    base = raw.get("base_export_id")
    return PublishedCopy(
        export_id=path.name,
        created_at=str(raw.get("created_at") or ""),
        mode=str(raw.get("mode") or ""),
        base_export_id=base if isinstance(base, str) and base else None,
    )


def _copies(settings: Settings, slug: str) -> list[Path]:
    if validate_slug(slug) is not None:
        raise ExportError(f"invalid repository slug: {slug!r}")
    repo_dir = root(settings) / slug
    if not repo_dir.is_dir() or repo_dir.is_symlink():
        return []
    return sorted(
        path
        for path in repo_dir.iterdir()
        if EXPORT_ID_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
    )


def slugs(settings: Settings) -> list[str]:
    base = root(settings)
    if not base.is_dir():
        return []
    return sorted(
        path.name
        for path in base.iterdir()
        if path.is_dir() and not path.is_symlink() and validate_slug(path.name) is None
    )


def published_copies(settings: Settings, slug: str) -> list[PublishedCopy]:
    """Marked copies of a repository, oldest first."""
    found = [copy for path in _copies(settings, slug) if (copy := _read_marker(path))]
    return sorted(found, key=lambda copy: (copy.created_at, copy.export_id))


def latest_published(settings: Settings, slug: str) -> str | None:
    """The export the next incremental export builds on, from the markers alone.

    Candidates are copies no other published copy uses as its base (the heads of the
    chains); the newest of them wins. Following the base links keeps the choice right
    even if the host clock once went backwards.
    """
    copies = published_copies(settings, slug)
    if not copies:
        return None
    used_as_base = {copy.base_export_id for copy in copies if copy.base_export_id}
    heads = [copy for copy in copies if copy.export_id not in used_as_base] or copies
    return max(heads, key=lambda copy: (copy.created_at, copy.export_id)).export_id


def backfill_markers(
    settings: Settings,
    slug: str,
    published: Collection[str],
) -> list[str]:
    """Mark copies that predate markers, if the export is known to be published.

    Published means: the database says so, or the export in the drop folder has .done.
    """
    marked: list[str] = []
    for path in _copies(settings, slug):
        if (path / PUBLISHED_MARKER).is_file():
            continue
        done = settings.export_dir / slug / path.name / ".done"
        if path.name not in published and not done.is_file():
            continue
        try:
            mark_published(settings, slug, path.name)
        except ExportError:
            continue
        marked.append(path.name)
    return marked


def remove(settings: Settings, slug: str, export_id: str) -> None:
    target = directory(settings, slug, export_id)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)


def prune(
    settings: Settings,
    protected: Mapping[str, str | Collection[str]],
    keep: int = DEFAULT_KEEP,
) -> int:
    """Keep the newest `keep` metadata copies per repository plus the protected bases.

    protected maps slug -> export_id(s) the next incremental export may need (newest
    published export by database and by markers), kept regardless of `keep`.
    """
    base = root(settings)
    if not base.is_dir():
        return 0
    removed = 0
    for repo_dir in base.iterdir():
        if repo_dir.is_symlink() or not repo_dir.is_dir():
            continue
        exports = sorted(
            (path for path in repo_dir.iterdir() if EXPORT_ID_RE.fullmatch(path.name)),
            key=lambda path: path.name,
        )
        guarded = protected.get(repo_dir.name, ())
        keep_ids = {guarded} if isinstance(guarded, str) else set(guarded)
        for path in exports[: max(len(exports) - max(keep, 1), 0)]:
            if path.name in keep_ids or path.is_symlink():
                continue
            shutil.rmtree(path)
            removed += 1
        # Interrupted copies; a fresh .tmp may belong to a save() in progress.
        cutoff = time.time() - STALE_TMP_S
        for leftover in repo_dir.glob("*.tmp"):
            if leftover.is_symlink() or not leftover.is_dir():
                continue
            if leftover.stat().st_mtime < cutoff:
                shutil.rmtree(leftover)
    return removed
