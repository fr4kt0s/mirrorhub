"""Dashboard: job counters, recent failures, storage usage, upcoming schedules."""

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, col, func, select

from mirrorhub.enums import JobStatus
from mirrorhub.models import Job, Repository
from mirrorhub.services import scheduler, storage
from mirrorhub.web.deps import CurrentUser, get_db, render, require_user

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    counts = {
        status.value: int(
            db.exec(
                select(func.count()).select_from(Job).where(col(Job.status) == status.value)
            ).one()
        )
        for status in JobStatus
    }
    failures = list(
        db.exec(
            select(Job)
            .where(col(Job.status) == JobStatus.FAILED.value)
            .order_by(col(Job.id).desc())
            .limit(5)
        ).all()
    )
    repo_names = {repo.id: repo.slug for repo in db.exec(select(Repository)).all()}
    upcoming = scheduler.upcoming(db, 5)
    return render(
        request,
        "dashboard.html",
        {
            "counts": counts,
            "failures": failures,
            "repo_names": repo_names,
            "upcoming": upcoming,
            "usage": storage.mount_usage(),
        },
    )
