"""Bounded SSE streaming for active jobs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.models import Job
from mirrorhub.services import logs

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
POLL_SECONDS = 0.75


def _event(
    name: str,
    value: dict[str, Any],
    event_id: int | None = None,
) -> str:
    identifier = f"id: {event_id}\n" if event_id is not None else ""
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return f"{identifier}event: {name}\ndata: {data}\n\n"


def _read(
    job_id: int,
    offset: int,
) -> tuple[dict[str, Any] | None, str, int, bool]:
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        if job is None:
            return None, "", offset, True

        state: dict[str, Any] = {
            "status": job.status,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "progress_pct": job.progress_pct,
            "progress_text": job.progress_text,
            "exit_code": job.exit_code,
            "error_summary": job.error_summary,
            "finished_at": (job.finished_at.isoformat() if job.finished_at else None),
        }
        path = Path(job.log_path) if job.log_path else None

    if path is None:
        return state, "", offset, True

    text, next_offset, eof = logs.read_chunk(path, offset)
    return state, text, next_offset, eof


async def job_events(
    job_id: int,
    request: Request,
    offset: int = 0,
) -> AsyncIterator[str]:
    resume = request.headers.get("last-event-id", "")
    if resume.isdigit():
        offset = int(resume)

    iterations = 0
    while not await request.is_disconnected():
        state, text, offset, eof = await asyncio.to_thread(
            _read,
            job_id,
            offset,
        )
        if state is None:
            yield _event(
                "status",
                {"status": "missing", "stream_complete": True},
            )
            return

        if text:
            yield _event("log", {"text": text}, event_id=offset)

        complete = state["status"] in TERMINAL and eof
        state["stream_complete"] = complete
        yield _event("status", state)

        if complete:
            return

        iterations += 1
        if iterations % 20 == 0:
            yield ": keep-alive\n\n"

        await asyncio.sleep(POLL_SECONDS if eof else 0)
