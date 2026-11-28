"""Job list, execution controls and bounded live logs."""

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.enums import TERMINAL_JOB_STATUSES, JobStatus, JobType, Tool, Trigger
from mirrorhub.models import Job
from mirrorhub.services import jobs as jobsvc
from mirrorhub.services import logs, repos
from mirrorhub.web import sse
from mirrorhub.web.deps import SESSION_COOKIE, CurrentUser, get_db, render, require_user

router = APIRouter()


@router.get("/jobs")
def list_page(
    request: Request,
    status: str = "",
    repo_id: int = 0,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Show the job list (filter by status and repository) with start buttons."""
    statement = select(Job).order_by(col(Job.id).desc()).limit(200)
    if status not in {item.value for item in JobStatus}:
        status = ""  # unknown filter values are ignored and never echoed into the page
    if status:
        statement = statement.where(col(Job.status) == status)
    if repo_id:
        statement = statement.where(Job.repo_id == repo_id)

    repositories = repos.list_all(db)
    return render(
        request,
        "jobs/list.html",
        {
            "jobs": list(db.exec(statement).all()),
            "repos": repositories,
            "repo_names": {repo.id: repo.slug for repo in repositories},
            "filters": {"status": status, "repo_id": repo_id},
            "statuses": [item.value for item in JobStatus],
        },
    )


@router.post("/jobs/{repo_id}/start")
def start(
    repo_id: int,
    request: Request,
    dry: int = 0,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Queue a sync or dry run (redirect to an already active one)."""
    repo = repos.get(db, repo_id)
    if repo is None:
        return render(
            request,
            "error.html",
            {"status": 404, "detail": "Repository not found"},
            404,
        )

    existing = jobsvc.find_active(db, repo.id or 0, JobType.SYNC.value)
    if existing is not None:
        return RedirectResponse(f"/jobs/{existing.id}", status_code=303)

    job = jobsvc.enqueue(
        db,
        repo,
        job_type=JobType.SYNC.value,
        trigger=Trigger.MANUAL.value,
        dry_run=bool(dry),
        requested_by=user.admin.username,
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/jobs/{repo_id}/rehash")
def rehash(
    repo_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Re-read every file of the repository and rebuild its hash cache."""
    repo = repos.get(db, repo_id)
    if repo is None:
        return render(
            request,
            "error.html",
            {"status": 404, "detail": "Repository not found"},
            404,
        )

    existing = jobsvc.find_active(db, repo.id or 0, JobType.REHASH.value)
    if existing is not None:
        return RedirectResponse(f"/jobs/{existing.id}", status_code=303)

    job = jobsvc.enqueue(
        db,
        repo,
        job_type=JobType.REHASH.value,
        trigger=Trigger.MANUAL.value,
        requested_by=user.admin.username,
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}")
def detail(
    job_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Show one job with its log."""
    job = jobsvc.get(db, job_id)
    if job is None:
        return render(
            request,
            "error.html",
            {"status": 404, "detail": "Job not found"},
            404,
        )

    repo = repos.get(db, job.repo_id)
    lines: list[str] = []
    offset = 0
    if job.log_path:
        lines, offset = logs.tail_snapshot(Path(job.log_path), lines=200)
    is_terminal = job.status in TERMINAL_JOB_STATUSES

    return render(
        request,
        "jobs/detail.html",
        {
            "job": job,
            "repo": repo,
            "lines": lines,
            "log_offset": offset,
            "is_terminal": is_terminal,
            "job_log": job.id is not None and logs.job_log_path(job.id).is_file(),
            "rsync_log": _rsync_log(job, repo.slug if repo else None, is_terminal) is not None
            or (job.tool == Tool.FTPSYNC.value and not is_terminal),
        },
    )


def _rsync_log(job: Job, slug: str | None, is_terminal: bool) -> Path | None:
    """Return the complete rsync log of an ftpsync job, if there is one.

    Once the job ended, the copy kept with the job; while it runs, the file
    ftpsync is writing.
    """
    if job.tool != Tool.FTPSYNC.value or job.id is None:
        return None
    kept = logs.kept_log_path(job.id, logs.RSYNC_LOG)
    if kept.is_file():
        return kept
    if not is_terminal and slug is not None:
        live = logs.ftpsync_rsync_log(slug)
        if live.is_file():
            return live
    return None


@router.get("/jobs/{job_id}/rsync-log")
def rsync_log(
    job_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download the complete rsync log of an ftpsync job (the job log shows 500 lines)."""
    job = jobsvc.get(db, job_id)
    if job is None:
        return render(request, "error.html", {"status": 404, "detail": "Job not found"}, 404)
    repo = repos.get(db, job.repo_id)
    slug = repo.slug if repo else None
    path = _rsync_log(job, slug, job.status in TERMINAL_JOB_STATUSES)
    if path is None:
        return render(
            request,
            "error.html",
            {"status": 404, "detail": "No rsync log for this job (yet)"},
            404,
        )
    name = f"job-{job_id}-rsync-{slug or job.repo_id}.log"
    return _download(request, path, name)


@router.get("/jobs/{job_id}/log")
def job_log(
    job_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download the complete job log of any job (the page shows the last 200 lines)."""
    job = jobsvc.get(db, job_id)
    if job is None or job.id is None:
        return render(request, "error.html", {"status": 404, "detail": "Job not found"}, 404)
    path = logs.job_log_path(job.id)
    if not path.is_file():
        return render(
            request, "error.html", {"status": 404, "detail": "No log for this job (yet)"}, 404
        )
    repo = repos.get(db, job.repo_id)
    name = f"job-{job.id}-{repo.slug if repo else job.repo_id}-{job.type}.log"
    return _download(request, path, name)


def _download(request: Request, path: Path, name: str) -> Response:
    """Stream a log file as an attachment, up to its size when the download starts.

    A running job keeps writing to the file; reading only what existed at the start
    keeps the response length consistent.
    """
    try:
        handle = path.open("rb")
        size = os.fstat(handle.fileno()).st_size
    except OSError:
        return render(request, "error.html", {"status": 404, "detail": "Log file gone"}, 404)

    def chunks() -> Iterator[bytes]:
        left = size
        with handle:
            while left > 0:
                data = handle.read(min(left, 1024 * 1024))
                if not data:
                    break
                left -= len(data)
                yield data

    return StreamingResponse(
        chunks(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/jobs/{job_id}/events")
def events(
    job_id: int,
    request: Request,
    offset: int = Query(default=0, ge=0),
    _user: CurrentUser = Depends(require_user),
) -> Response:
    """Stream a job's log and status to the job page (server-sent events)."""
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        if job is None:
            return Response("not found", status_code=404)

        # A completed job needs no live stream. The detail page already
        # displays its bounded final tail.
        if job.status in TERMINAL_JOB_STATUSES:
            return Response(status_code=204)

    return StreamingResponse(
        sse.job_events(job_id, request, offset, request.cookies.get(SESSION_COOKIE, "")),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/jobs/{job_id}/cancel")
def cancel(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Cancel a queued job or ask the worker to stop a running one."""
    job = jobsvc.get(db, job_id)
    if job is None:
        return render(
            request,
            "error.html",
            {"status": 404, "detail": "Job not found"},
            404,
        )

    jobsvc.request_cancel(db, job, actor=user.admin.username)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/cleanup")
def cleanup(
    status: str = Form(""),
    repo_id: int = Form(0),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete finished jobs (optionally of one status or repository)."""
    jobsvc.delete_finished(
        db,
        actor=user.admin.username,
        finished=TERMINAL_JOB_STATUSES,
        status=status,
        repo_id=repo_id,
    )
    params = {k: v for k, v in (("status", status), ("repo_id", repo_id)) if v}
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/jobs{query}", status_code=303)


@router.post("/jobs/{job_id}/delete")
def delete(
    job_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete one finished job and its log."""
    job = jobsvc.get(db, job_id)
    if job is None:
        return render(request, "error.html", {"status": 404, "detail": "Job not found"}, 404)
    jobsvc.delete_job(db, job, actor=user.admin.username, finished=TERMINAL_JOB_STATUSES)
    return RedirectResponse("/jobs", status_code=303)
