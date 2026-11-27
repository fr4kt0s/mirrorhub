"""Repair exports (#43): answer a receiver's repair request.

The receiver's importer writes `repair-<slug>.json` and `repair-<slug>.json.sha256`
when its tree differs from the last applied export (see docs/importer.md). Uploaded
on the Exports page, the request is checked, kept on the state volume and turned
into an export job: an incremental export since the request's `base_export_id` that

- carries every `resend` path that still exists on the sender, even if unchanged
  since the base,
- lists every `delete` path that does not exist on the sender in deleted.txt
  (unless the operator keeps the receiver's extra files),

and records the request in the manifest field `repair`. Importers that know the
field compare the whole tree after applying; older ones apply it as an ordinary
incremental export. The export format is otherwise unchanged.

Only the standard library and the manifest module are used here, so the checks run
without the web application.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mirrorhub.export.manifest import ExportError, Inventory, validate_relative

FORMAT = "mirrorhub-repair-request"
VERSION = 1
MAX_BYTES = 16 * 1024 * 1024  # the default request body limit of the proxy
KEEP_SECONDS = 7 * 24 * 3600  # requests that never became an export
EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-(full|incr)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Request:
    """A checked repair request of a receiver (format ``mirrorhub-repair-request`` v1)."""

    slug: str
    base_export_id: str
    created_at: str
    check: str
    importer_commit: str | None
    resend: tuple[str, ...]
    delete: tuple[str, ...]
    skipped: int
    sha256: str  # of the request file as uploaded


def request_name(slug: str) -> str:
    """Return the file name the importer gives a repair request (``repair-<slug>.json``)."""
    return f"repair-{slug}.json"


def _paths(document: dict[str, Any], key: str) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(path, str) for path in value):
        raise ExportError(f"repair request: {key} must be a list of paths")
    if len(set(value)) != len(value):
        raise ExportError(f"repair request: {key} lists a path twice")
    for path in value:
        try:
            validate_relative(path)
        except ExportError as exc:
            raise ExportError(f"repair request: {key}: {exc}") from exc
    return tuple(sorted(value))


def parse(data: bytes, checksum: bytes, slug: str) -> Request:
    """Check an uploaded request and its .sha256 file; the request or ExportError."""
    if not data:
        raise ExportError("the repair request is empty")
    if len(data) > MAX_BYTES:
        raise ExportError("the repair request exceeds the size limit")
    digest = hashlib.sha256(data).hexdigest()
    try:
        line = checksum.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ExportError("the .sha256 file is not a checksum line") from exc
    expected, _, name = line.partition("  ")
    if not SHA256_RE.fullmatch(expected) or name.lstrip("*") != request_name(slug):
        raise ExportError(
            f"the .sha256 file must be one line '<sha256>  {request_name(slug)}' "
            "(upload the request of this repository)"
        )
    if expected != digest:
        raise ExportError("the request does not match its .sha256 file (damaged on the way?)")
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ExportError(f"the repair request is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ExportError("the repair request is not a JSON object")
    if document.get("format") != FORMAT:
        raise ExportError("not a MirrorHub repair request")
    if document.get("version") != VERSION:
        raise ExportError(f"unsupported repair request version {document.get('version')!r}")
    if document.get("slug") != slug:
        raise ExportError(f"the request is for repository {document.get('slug')!r}, not {slug!r}")
    base = document.get("base_export_id")
    if not isinstance(base, str) or not EXPORT_ID_RE.fullmatch(base):
        raise ExportError("repair request: invalid base_export_id")
    resend = _paths(document, "resend")
    delete = _paths(document, "delete")
    if set(resend) & set(delete):
        raise ExportError("repair request: a path is listed to resend and to delete")
    skipped = document.get("skipped", 0)
    if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
        raise ExportError("repair request: skipped must be a count")
    if not resend and not delete:
        raise ExportError("the repair request lists nothing to repair")
    commit = document.get("importer_commit")
    return Request(
        slug=slug,
        base_export_id=base,
        created_at=str(document.get("created_at") or ""),
        check=str(document.get("check") or ""),
        importer_commit=commit if isinstance(commit, str) else None,
        resend=resend,
        delete=delete,
        skipped=skipped,
        sha256=digest,
    )


def check_against_base(request: Request, base: Inventory) -> None:
    """Check the request's paths against the base inventory (ExportError if inconsistent).

    The receiver compared its tree with the base's files.json: what it misses or has
    differently must be listed there, what it has extra must not.
    """
    unknown = [path for path in request.resend if path not in base]
    if unknown:
        raise ExportError(
            f"repair request: {len(unknown)} path(s) to resend are not in "
            f"{request.base_export_id}, e.g. {unknown[0]}"
        )
    listed = [path for path in request.delete if path in base]
    if listed:
        raise ExportError(
            f"repair request: {len(listed)} extra path(s) are in "
            f"{request.base_export_id}, e.g. {listed[0]}"
        )


# ---- kept requests (state volume) -----------------------------------------------------


def _directory(root: Path, slug: str) -> Path:
    return root / "repair-requests" / slug


def store(root: Path, request: Request, data: bytes) -> Path:
    """Keep the uploaded request until its export is published (named by its SHA-256).

    Requests older than a week that never became an export are removed here.
    """
    directory = _directory(root, request.slug)
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - KEEP_SECONDS
    for old in directory.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except FileNotFoundError:
            pass
    path = directory / f"{request.sha256}.json"
    temporary = directory / f".{request.sha256}.tmp"
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def load(root: Path, slug: str, sha256: str) -> Request:
    """Load a kept request by its SHA-256 and check it again."""
    if not SHA256_RE.fullmatch(sha256):
        raise ExportError("invalid repair request id")
    path = _directory(root, slug) / f"{sha256}.json"
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ExportError("the repair request is no longer kept; upload it again") from exc
    return parse(data, f"{sha256}  {request_name(slug)}\n".encode(), slug)


def discard(root: Path, slug: str, sha256: str) -> None:
    """Remove a kept request (after its export was published)."""
    if SHA256_RE.fullmatch(sha256):
        (_directory(root, slug) / f"{sha256}.json").unlink(missing_ok=True)


# ---- the export -----------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """A repair request applied to an export's payload and deletion list."""

    changed: list[str]
    deleted: list[str]
    resent: int  # requested paths added to the payload
    removed: int  # requested paths added to deleted.txt
    field: dict[str, Any]  # manifest field "repair"


def apply(
    request: Request,
    current: Inventory,
    changed: Iterable[str],
    deleted: Iterable[str],
    *,
    keep_extra: bool,
) -> Applied:
    """Payload and deletions of the export, widened by the request.

    A path to resend that the sender no longer has is already in deleted.txt (it was
    in the base). An extra path the sender has now is in the payload anyway.
    """
    resend = [path for path in request.resend if path in current]
    remove = [] if keep_extra else [path for path in request.delete if path not in current]
    field = {
        "request_sha256": request.sha256,
        "requested_at": request.created_at,
        "check": request.check,
        "resend": len(resend),
        "delete": len(remove),
        "keep_extra": keep_extra,
        "skipped": request.skipped,
    }
    return Applied(
        changed=sorted(set(changed) | set(resend)),
        deleted=sorted(set(deleted) | set(remove)),
        resent=len(resend),
        removed=len(remove),
        field=field,
    )
