"""Event files for monitoring: one JSON file per event in events_dir.

Same idea as the sender's file notification channel (`<drop folder>/_events/`), for
the receiver side. contrib/checkmk/mirrorhub_events.py reads both directories and
turns them into Checkmk services (see docs/checkmk.md).

Enabled by `events_dir` in importer.toml. Each file is written to a hidden
temporary name and renamed, so a reader never sees a partial file. Files older
than `events_keep_days` (default 30) are deleted on the next run; the Checkmk
check keeps its own state, so it does not need old files.

Events (common fields: schema, event, time (UTC), source "importer", host, repo,
importer_version):

    import_applied     export_id, export_mode, base_export_id, payload_count,
                       deleted_count, duration_s, tree_verified (whole tree compared)
    import_adopted     export_id, export_mode (adopt wrote a lost state file)
    import_superseded  export_id, superseded_by (skipped, a later export covers it)
    import_failed      export_id (if known), error, error_kind, exit_code
    tree_check         export_id, origin (import, post-check, check, adopt), hashed,
                       differences, counts (per kind: missing, extra, ...), entries
    check_failed       error, error_kind, exit_code
    import_run         scope (all, slug <slug>, export <path>), exit_code, duration_s;
                       one per `import` run, also when nothing was pending (heartbeat)

Writing an event never fails an import: errors are logged as warnings.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from mirrorhub import buildinfo
from mirrorhub.importer.logs import log_event

SCHEMA = 1
DEFAULT_KEEP_DAYS = 30

_directory: Path | None = None
_keep_days = DEFAULT_KEEP_DAYS
_sequence = 0


def configure(directory: Path | None, keep_days: int = DEFAULT_KEEP_DAYS) -> None:
    """Enable event files in directory (None disables them) and prune old ones."""
    global _directory, _keep_days
    _directory = directory
    _keep_days = keep_days
    if directory is not None:
        prune()


def enabled() -> bool:
    return _directory is not None


def _name(now: datetime, kind: str, repo: str | None) -> str:
    global _sequence
    _sequence += 1
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
    return f"{stamp}-{os.getpid()}-{_sequence:04d}-importer-{repo or '_'}-{kind}.json"


def emit(kind: str, repo: str | None, **fields: object) -> Path | None:
    """Write one event file; returns its path, or None if disabled or failed."""
    if _directory is None:
        return None
    now = datetime.now(UTC)
    document: dict[str, object] = {
        "schema": SCHEMA,
        "event": kind,
        "time": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "importer",
        "host": socket.gethostname(),
        "repo": repo,
        "importer_version": buildinfo.describe(),
    }
    document.update(fields)
    target = _directory / _name(now, kind, repo)
    temporary = _directory / f".{target.name}.tmp"
    try:
        _directory.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, default=str)
            handle.write("\n")
        os.replace(temporary, target)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        log_event(logging.WARNING, "event file not written", event_kind=kind, error=str(exc))
        return None
    log_event(logging.DEBUG, "event file written", event_kind=kind, path=target)
    return target


def prune() -> int:
    """Delete event files (and stale temporary files) older than keep_days."""
    if _directory is None or _keep_days <= 0 or not _directory.is_dir():
        return 0
    limit = time.time() - _keep_days * 86400
    removed = 0
    for path in _directory.iterdir():
        if not (path.name.endswith(".json") or path.name.endswith(".json.tmp")):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log_event(logging.DEBUG, "old event files removed", count=removed, keep_days=_keep_days)
    return removed


def count_kinds(found: list[str]) -> dict[str, int]:
    """Differences per kind ("missing: a/b" -> {"missing": 1})."""
    counts: dict[str, int] = {}
    for line in found:
        kind = line.partition(": ")[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts
