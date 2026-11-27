"""Export history, manual export actions and retention."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.datastructures import FormData, UploadFile

from mirrorhub.export import repair
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Repository
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
    """Show the Exports page: start exports, history, retention and drop-folder state."""
    repositories = repos.list_all(db)
    keep = export_jobs.keep_chains(db)
    plan = export_jobs.retention_plan(db, keep)
    exports = export_jobs.list_exports(db)
    names = {repo.id: repo.slug for repo in repositories}
    settings = get_settings()
    drop_folder = {
        row.id: export_jobs.drop_folder_state(settings, names[row.repo_id], row.export_id)
        for row in exports
        if row.status == "published" and row.repo_id in names
    }
    base_choices = {
        repo.id: [
            {
                "export_id": copy.export_id,
                "mode": copy.mode,
                "drop_folder": export_jobs.drop_folder_state(settings, repo.slug, copy.export_id),
            }
            for copy in export_jobs.base_choices(settings, repo.slug)
        ]
        for repo in repositories
    }
    return render(
        request,
        "exports.html",
        {
            "exports": exports,
            "drop_folder": drop_folder,
            "superseded": export_jobs.superseded(exports),
            "base_choices": base_choices,
            "meta_keep": settings.export_meta_keep,
            "repositories": repositories,
            "repo_names": names,
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
    """Queue a full or incremental export (optionally since a chosen earlier export)."""
    form = await request.form()
    mode = str(form.get("mode") or "")
    if mode not in ("full", "incr"):
        return Response("mode must be full or incr", status_code=400)
    base = str(form.get("base") or "").strip() or None
    if base is not None and mode != "incr":
        return Response(
            "an export since an earlier export is always incremental",
            status_code=400,
            media_type="text/plain",
        )

    try:
        job_id = export_jobs.enqueue(
            repo_id,
            mode=mode,
            actor=user.admin.username,
            ip=client_ip(request),
            base_export_id=base,
        )
    except ExportError as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


async def _upload(form: FormData, name: str) -> bytes:
    value = form.get(name)
    if not isinstance(value, UploadFile):
        return b""
    return await value.read(repair.MAX_BYTES + 1)


def _repair_page(
    request: Request,
    repo: Repository,
    *,
    errors: list[str] | None = None,
    plan: export_jobs.RepairPlan | None = None,
    keep_extra: bool = False,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "export_repair.html",
        {"repo": repo, "errors": errors or [], "plan": plan, "keep_extra": keep_extra},
        status_code=status_code,
    )


@router.post("/exports/{repo_id}/repair")
async def repair_upload(
    repo_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Repair export (#43), step 1: check the uploaded request and show what it does."""
    repo = db.get(Repository, repo_id)
    if repo is None:
        return Response("repository not found", status_code=404, media_type="text/plain")
    form = await request.form()
    keep_extra = form.get("keep_extra") == "on"
    data = await _upload(form, "request")
    checksum = await _upload(form, "checksum")
    settings = get_settings()
    try:
        if not checksum:
            raise ExportError(
                f"select the request {repair.request_name(repo.slug)} and its .sha256 file"
            )
        parsed = repair.parse(data, checksum[:4096], repo.slug)
        plan = export_jobs.plan_repair(settings, repo.slug, parsed)
        repair.store(settings.manifest_dir, parsed, data)
    except (ExportError, OSError) as exc:
        return _repair_page(request, repo, errors=[str(exc)], status_code=400)
    return _repair_page(request, repo, plan=plan, keep_extra=keep_extra)


@router.post("/exports/{repo_id}/repair/start")
async def repair_start(
    repo_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Repair export, step 2: queue the export for the shown request."""
    repo = db.get(Repository, repo_id)
    if repo is None:
        return Response("repository not found", status_code=404, media_type="text/plain")
    form = await request.form()
    try:
        job_id = export_jobs.enqueue(
            repo_id,
            mode="incr",
            actor=user.admin.username,
            ip=client_ip(request),
            repair_request=str(form.get("request") or ""),
            keep_extra=form.get("keep_extra") == "on",
        )
    except ExportError as exc:
        return _repair_page(request, repo, errors=[str(exc)], status_code=400)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/exports/retention/save")
async def retention_save(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Save the "chains to keep" setting."""
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
    """Remove the exports retention selects now."""
    try:
        export_jobs.apply_retention(
            db,
            get_settings(),
            actor=user.admin.username,
            ip=client_ip(request),
        )
    except (ExportError, OSError) as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    return RedirectResponse("/exports", status_code=303)


@router.post("/exports/failed/cleanup")
def failed_cleanup(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Remove failed export records and their unfinished directories."""
    try:
        export_jobs.delete_failed(
            db, get_settings(), actor=user.admin.username, ip=client_ip(request)
        )
    except (ExportError, OSError) as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    return RedirectResponse("/exports", status_code=303)
