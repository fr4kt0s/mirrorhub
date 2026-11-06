"""Storage dashboard: mount usage from statvfs plus cached per-repository sizes."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.services import repos, storage
from mirrorhub.web.deps import CurrentUser, get_db, render, require_user

router = APIRouter()


@router.get("/storage")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    cache = storage.cached(db)
    rows = [{"repo": repo, "cache": cache.get(repo.id or 0)} for repo in repos.list_all(db)]
    return render(
        request,
        "storage.html",
        {"usage": storage.mount_usage(), "rows": rows, "refreshing": storage.is_refreshing()},
    )


@router.post("/storage/refresh")
def refresh(
    _user: CurrentUser = Depends(require_user),
) -> Response:
    storage.start_refresh()
    return RedirectResponse("/storage", status_code=303)
