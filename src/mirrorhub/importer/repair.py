"""Repair requests: what the receiver needs from the sender to fix its tree (#43).

When a tree check finds differences, the report directory gets, next to the lists,

    <reports>/<slug>/repair-<slug>.json         the request
    <reports>/<slug>/repair-<slug>.json.sha256  its checksum (sha256sum -c format)

The request names the export the receiver is on and the paths it needs: `resend`
(missing, wrong type, size, mtime, symlink target or SHA-256) and `delete` (extra
paths). Carried to the sender and uploaded on the Exports page, it becomes a repair
export: an incremental export since that export which also carries the `resend` paths
and deletes the `delete` paths. Without differences both files are removed.

Paths that cannot travel in an export (file names that are not valid UTF-8 or contain
control characters, possible only for extra paths) are left out and counted in
`skipped`; extra.txt lists them, remove them by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mirrorhub import buildinfo
from mirrorhub.importer.checks import safe_relative

FORMAT = "mirrorhub-repair-request"
VERSION = 1
# kinds of the tree comparison (tree.differences, content_differences)
RESEND_KINDS = frozenset({"missing", "type", "size", "mtime", "symlink target", "sha256"})
DELETE_KINDS = frozenset({"extra"})


def request_name(slug: str) -> str:
    """Return the file name of a repository's repair request."""
    return f"repair-{slug}.json"


def request_path(report_dir: Path, slug: str) -> Path:
    """Where the request of a slug lies (the slug's report directory)."""
    return report_dir / slug / request_name(slug)


def _travels(path: str) -> bool:
    """Return True if the path can be named in an export (files.json, deleted.txt)."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:  # undecodable bytes kept as surrogates
        return False
    return safe_relative(path)


def build_request(
    slug: str, export_id: str, found: list[str], *, hashed: bool
) -> dict[str, Any] | None:
    """Build the request document for the differences `found`; None if there are none."""
    resend: set[str] = set()
    delete: set[str] = set()
    skipped = 0
    for line in found:
        kind, _, path = line.partition(": ")
        if kind not in RESEND_KINDS and kind not in DELETE_KINDS:
            continue
        if not _travels(path):
            skipped += 1
            continue
        (resend if kind in RESEND_KINDS else delete).add(path)
    if not resend and not delete and not skipped:
        return None
    return {
        "format": FORMAT,
        "version": VERSION,
        "slug": slug,
        "base_export_id": export_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "check": "quick+sha256" if hashed else "quick",
        "importer_commit": buildinfo.commit(),
        "resend": sorted(resend),
        "delete": sorted(delete),
        "skipped": skipped,
    }


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_request(
    report_dir: Path, slug: str, export_id: str, found: list[str], *, hashed: bool
) -> Path | None:
    """Write (or, without differences, remove) the request; its path or None."""
    path = request_path(report_dir, slug)
    checksum = path.with_name(path.name + ".sha256")
    document = build_request(slug, export_id, found, hashed=hashed)
    if document is None:
        for stale in (path, checksum):
            stale.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, indent=1, sort_keys=False) + "\n").encode("ascii")
    _write_bytes(path, data)
    _write_bytes(checksum, f"{hashlib.sha256(data).hexdigest()}  {path.name}\n".encode("ascii"))
    return path


def count(document: dict[str, Any]) -> str:
    """Short text for output: '3 to resend, 1 to delete'."""
    text = f"{len(document['resend'])} to resend, {len(document['delete'])} to delete"
    if document.get("skipped"):
        text += f", {document['skipped']} not transferable (see extra.txt)"
    return text


def read_request(path: Path) -> dict[str, Any] | None:
    """Read a repair request document; None if it is missing or not valid JSON."""
    try:
        document = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None
