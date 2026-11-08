"""Export history, manual export actions and retention."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.export.manifest import ExportError
from mirrorhub.services import appsettings, audit, export_jobs, repos
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


@router.get("/exports")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    repositories = repos.list_all(db)
    keep = export_jobs.keep_chains(db)
    plan = export_jobs.retention_plan(db, keep)
    return render(
        request,
        "exports.html",
        {
            "exports": export_jobs.list_exports(db),
            "repositories": repositories,
            "repo_names": {repo.id: repo.slug for repo in repositories},
            "failed_count": len(export_jobs.failed_exports(db)),
            "retention": {
                "keep": keep,
                "count": len(plan.exports),
                "bytes": plan.total_bytes,
            },
        },
    )


@router.post("/exports/{repo_id}/start")
async def start(
    repo_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> Response:
    form = await request.form()
    mode = str(form.get("mode") or "")
    if mode not in ("full", "incr"):
        return Response("mode must be full or incr", status_code=400)

    try:
        job_id = export_jobs.enqueue(
            repo_id,
            mode=mode,
            actor=user.admin.username,
            ip=client_ip(request),
        )
    except ExportError as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/exports/retention/save")
async def retention_save(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        keep = int(str(form.get("keep_chains") or "0"))
    except ValueError:
        return Response("keep_chains must be a number", status_code=400, media_type="text/plain")
    if not 0 <= keep <= 100:
        return Response("keep_chains must be 0-100", status_code=400, media_type="text/plain")

    appsettings.set_value(db, export_jobs.RETENTION_KEY, keep, commit=False)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="settings.update",
        target_type="setting",
        target_id=export_jobs.RETENTION_KEY,
        detail={"keep_chains": keep},
        commit=False,
    )
    db.commit()
    return RedirectResponse("/exports", status_code=303)


@router.post("/exports/retention/apply")
def retention_apply(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        export_jobs.apply_retention(
            db,
            get_settings(),
            actor=user.admin.username,
            ip=client_ip(request),
        )
    except ExportError as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    except (ExportError, OSError) as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    return RedirectResponse("/exports", status_code=303)


@router.post("/exports/failed/cleanup")
def failed_cleanup(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        export_jobs.delete_failed(
            db, get_settings(), actor=user.admin.username, ip=client_ip(request)
        )
    except (ExportError, OSError) as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    return RedirectResponse("/exports", status_code=303)
