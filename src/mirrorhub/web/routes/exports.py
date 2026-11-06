"""Export history and manual export actions."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.export.manifest import ExportError
from mirrorhub.services import export_jobs, repos
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


@router.get("/exports")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    repositories = repos.list_all(db)
    return render(
        request,
        "exports.html",
        {
            "exports": export_jobs.list_exports(db),
            "repositories": repositories,
            "repo_names": {repo.id: repo.slug for repo in repositories},
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
