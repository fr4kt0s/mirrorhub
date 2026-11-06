"""Job list, execution controls and bounded live logs."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus, JobType, Trigger
from mirrorhub.models import Job
from mirrorhub.services import jobs as jobsvc
from mirrorhub.services import logs, repos
from mirrorhub.web import sse
from mirrorhub.web.deps import CurrentUser, get_db, render, require_user

router = APIRouter()


@router.get("/jobs")
def list_page(
    request: Request,
    status: str = "",
    repo_id: int = 0,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    statement = select(Job).order_by(col(Job.id).desc()).limit(200)
    if status in {item.value for item in JobStatus}:
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


@router.get("/jobs/{job_id}")
def detail(
    job_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
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

    return render(
        request,
        "jobs/detail.html",
        {
            "job": job,
            "repo": repo,
            "lines": lines,
            "log_offset": offset,
            "is_terminal": job.status in sse.TERMINAL,
        },
    )


@router.get("/jobs/{job_id}/events")
def events(
    job_id: int,
    request: Request,
    offset: int = Query(default=0, ge=0),
    _user: CurrentUser = Depends(require_user),
) -> Response:
    with Session(get_engine()) as db:
        job = db.get(Job, job_id)
        if job is None:
            return Response("not found", status_code=404)

        # A completed job needs no live stream. The detail page already
        # displays its bounded final tail.
        if job.status in sse.TERMINAL:
            return Response(status_code=204)

    return StreamingResponse(
        sse.job_events(job_id, request, offset),
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
