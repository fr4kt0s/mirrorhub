"""Preset list, cloning into a prefilled form, saving a repository as a preset."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.services import audit, presets
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


@router.get("/presets")
def list_page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return render(request, "presets/list.html", {"presets": presets.list_all(db)})


@router.post("/presets/{preset_id}/delete")
def delete_preset(
    preset_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    row = presets.get(db, preset_id)
    if row is not None:
        presets.delete(db, row, actor=user.admin.username, ip=client_ip(request))
    return RedirectResponse("/presets", status_code=303)


@router.post("/presets/reseed")
def reseed(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    created = presets.seed(db, restore=True)
    if created:
        audit.record(
            db,
            actor=user.admin.username,
            ip=client_ip(request),
            action="preset.reseed",
            target_type="preset",
            target_id="*",
            detail={"created": created},
        )
    return RedirectResponse("/presets", status_code=303)
