"""Bounded job-log reads and retention."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from mirrorhub.settings import Settings, get_settings

log = logging.getLogger("mirrorhub.logs")

TAIL_MAX_BYTES = 256 * 1024
STREAM_MAX_BYTES = 64 * 1024


def job_log_path(
    job_id: int,
    settings: Settings | None = None,
) -> Path:
    base = (settings or get_settings()).log_dir / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{job_id}.log"


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


def read_new(path: Path, offset: int) -> tuple[list[str], int]:
    """Compatibility wrapper; reads are now bounded."""
    text, next_offset, _eof = read_chunk(path, offset)
    return text.splitlines(), next_offset


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
