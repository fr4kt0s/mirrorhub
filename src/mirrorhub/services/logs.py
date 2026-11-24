"""Bounded job-log reads and retention."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from mirrorhub.settings import Settings, get_settings

log = logging.getLogger("mirrorhub.logs")

TAIL_MAX_BYTES = 256 * 1024
STREAM_MAX_BYTES = 64 * 1024


RUN_HEADER_PREFIX = "# run job "


def job_log_path(
    job_id: int,
    settings: Settings | None = None,
) -> Path:
    base = (settings or get_settings()).log_dir / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{job_id}.log"


def run_header(
    job_id: int,
    attempt: int,
    repo: str,
    job_type: str,
    tool: str,
    worker: str,
    started: datetime,
) -> str:
    """First line of every run, so a log is self-describing and the current run can be
    cut out of it (retries append to the same file)."""
    # models.utcnow() returns naive UTC; astimezone() would read it as local time.
    aware = started.replace(tzinfo=UTC) if started.tzinfo is None else started.astimezone(UTC)
    stamp = aware.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{RUN_HEADER_PREFIX}{job_id} attempt {attempt} repo {repo} "
        f"type {job_type} tool {tool} worker {worker} started {stamp}\n"
    )


def quarantine_stale(path: Path) -> Path | None:
    """Move a log that exists before the job's first run out of the way.

    That file belongs to an earlier job with the same id (ids were reused before
    migration 0006). It keeps the .log suffix so the retention sweep removes it.
    """
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = path.with_name(f"{path.stem}.stale-{stamp}.log")
    path.replace(target)
    log.warning(
        "stale job log moved aside",
        extra={"path": str(path), "moved_to": str(target)},
    )
    return target


def remove_job_logs(
    job_ids: Iterable[int],
    settings: Settings | None = None,
) -> int:
    """Delete the log files of deleted jobs. Missing files are fine."""
    base = (settings or get_settings()).log_dir / "jobs"
    removed = 0
    for job_id in job_ids:
        try:
            (base / f"{int(job_id)}.log").unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("could not remove job log", extra={"job_id": job_id})
    return removed


def tail_snapshot(
    path: Path,
    lines: int = 200,
    max_bytes: int = TAIL_MAX_BYTES,
) -> tuple[list[str], int]:
    """Return a bounded tail and the byte offset immediately after it."""
    if lines <= 0 or max_bytes <= 0:
        return [], 0

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - max_bytes)
            handle.seek(start)
            data = handle.read(end - start)
    except FileNotFoundError:
        return [], 0

    if start > 0:
        first_newline = data.find(b"\n")
        if first_newline >= 0:
            data = data[first_newline + 1 :]

    return data.decode("utf-8", "replace").splitlines()[-lines:], end


def tail(path: Path, lines: int = 200) -> list[str]:
    values, _offset = tail_snapshot(path, lines)
    return values


def tail_current_run(path: Path, lines: int = 200) -> list[str]:
    """Like tail(), but starting no earlier than the last run header.

    Logs written before run headers existed have none; then this equals tail().
    """
    values = tail(path, lines)
    for index in range(len(values) - 1, -1, -1):
        if values[index].startswith(RUN_HEADER_PREFIX):
            return values[index:]
    return values


def _trim_partial_utf8(data: bytes) -> bytes:
    """Drop an incomplete trailing UTF-8 sequence from a chunk."""
    for cut in range(min(3, len(data)) + 1):
        candidate = data[: len(data) - cut]
        if not candidate and cut:
            break
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return candidate
    return data


def read_chunk(
    path: Path,
    offset: int,
    max_bytes: int = STREAM_MAX_BYTES,
) -> tuple[str, int, bool]:
    """Read a bounded byte chunk and return text, next offset and EOF state."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            position = max(0, offset)
            if position > size:
                position = 0

            handle.seek(position)
            data = handle.read(max_bytes)

            if data and position + len(data) < size:
                data = _trim_partial_utf8(data)

            next_offset = position + len(data)
            return (
                data.decode("utf-8", "replace"),
                next_offset,
                next_offset >= size,
            )
    except FileNotFoundError:
        return "", 0, True


def retention_sweep(
    settings: Settings | None = None,
    days: int = 30,
) -> int:
    settings = settings or get_settings()
    base = settings.log_dir / "jobs"
    if not base.is_dir():
        return 0

    cutoff = time.time() - days * 86400
    removed = 0
    for path in base.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue

    if removed:
        log.info(
            "job logs removed",
            extra={"count": removed, "days": days},
        )
    return removed
