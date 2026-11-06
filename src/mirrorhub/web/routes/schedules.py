"""Schedule CRUD routes."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, select

from mirrorhub.models import Schedule
from mirrorhub.security import validators
from mirrorhub.services import audit, repos, scheduler
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


def _page(
    request: Request,
    db: Session,
    errors: list[str] | None = None,
) -> Response:
    schedules = list(db.exec(select(Schedule).order_by(col(Schedule.id))).all())
    return render(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "repositories": repos.list_all(db),
            "presets": scheduler.PRESETS,
            "errors": errors or [],
        },
    )


@router.get("/schedules")
def list_page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return _page(request, db)


@router.post("/schedules")
async def create(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        repo_id = int(str(form.get("repo_id") or 0))
    except ValueError:
        return _page(request, db, ["select a valid repository"])

    cron = str(form.get("cron_expr") or "").strip()
    error = validators.validate_cron(cron)
    if error or repo_id <= 0:
        return _page(request, db, [error or "select a repository"])

    repo = repos.get(db, repo_id)
    if repo is None:
        return _page(request, db, ["unknown repository"])

    job_type = str(form.get("job_type") or "sync")
    if job_type not in ("sync", "export"):
        return _page(request, db, ["job type must be sync or export"])

    schedule = Schedule(
        repo_id=repo_id,
        job_type=job_type,
        cron_expr=cron,
        preset_label=str(form.get("preset_label") or ""),
        enabled=bool(form.get("enabled")),
        chain_export=bool(form.get("chain_export")),
        next_fire_at=scheduler.next_after(cron),
    )
    db.add(schedule)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="schedule.create",
        target_type="schedule",
        target_id=repo.slug,
        detail={"cron": cron, "job_type": job_type},
        commit=False,
    )
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}")
async def update(
    schedule_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        return _page(request, db, ["unknown schedule"])

    form = await request.form()
    cron = str(form.get("cron_expr") or "").strip()
    error = validators.validate_cron(cron)
    if error:
        return _page(request, db, [error])

    schedule.cron_expr = cron
    schedule.enabled = bool(form.get("enabled"))
    schedule.chain_export = bool(form.get("chain_export"))
    schedule.preset_label = str(form.get("preset_label") or "")
    schedule.next_fire_at = scheduler.next_after(cron)
    db.add(schedule)

    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="schedule.update",
        target_type="schedule",
        target_id=str(schedule_id),
        detail={"cron": cron},
        commit=False,
    )
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete(
    schedule_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is not None:
        audit.record(
            db,
            actor=user.admin.username,
            ip=client_ip(request),
            action="schedule.delete",
            target_type="schedule",
            target_id=str(schedule_id),
            detail={"cron": schedule.cron_expr},
            commit=False,
        )
        db.delete(schedule)
        db.commit()

    return RedirectResponse("/schedules", status_code=303)
