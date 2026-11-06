"""GPG key page: list, upload, import from a package keyring, delete."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from mirrorhub.services import audit, gpg
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


def _page(request: Request, db: Session, errors: list[str] | None = None) -> Response:
    try:
        rows = gpg.sync_rows(db)
        available = gpg.package_candidates()
        error_list = errors or []
    except gpg.GpgError as exc:
        rows, available, error_list = [], [], [str(exc)]
    return render(
        request,
        "keys.html",
        {"keys": rows, "packages": available, "errors": error_list},
    )


@router.get("/keys")
def list_page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return _page(request, db)


@router.post("/keys/upload")
async def upload(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    upload_file = form.get("keyfile")
    if not hasattr(upload_file, "read"):
        return _page(request, db, ["Select a .gpg or .asc file to upload"])
    data = await upload_file.read()  # type: ignore[union-attr]
    if not data:
        return _page(request, db, ["The uploaded file is empty"])
    if len(data) > get_settings().max_gpg_upload_bytes:
        return _page(request, db, ["The uploaded file exceeds the size limit"])
    try:
        imported = gpg.import_key_bytes(data)
    except gpg.GpgError as exc:
        return _page(request, db, [f"Import failed: {exc}"])
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="gpg.key_import",
        target_type="gpg_key",
        target_id=",".join(key.fingerprint for key in imported)[:128],
        detail={"source": "upload", "count": len(imported)},
    )
    return _page(request, db)


@router.post("/keys/import-package")
async def import_package(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    name = Path(str(form.get("package") or "")).name
    allowed = {path.name for path in gpg.package_candidates()}
    if name not in allowed:
        return _page(request, db, ["Unknown keyring package file"])
    target = Path("/usr/share/keyrings") / name
    try:
        imported = gpg.import_key_file(target)
    except gpg.GpgError as exc:
        return _page(request, db, [f"Import failed: {exc}"])
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="gpg.key_import",
        target_type="gpg_key",
        target_id=",".join(key.fingerprint for key in imported)[:128],
        detail={"source": "package", "package": name, "count": len(imported)},
    )
    return _page(request, db)


@router.post("/keys/{fingerprint}/delete")
def delete(
    fingerprint: str,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not fingerprint.isalnum() or len(fingerprint) < 16:
        return _page(request, db, ["Invalid fingerprint"])
    try:
        gpg.delete_key(fingerprint)
    except gpg.GpgError as exc:
        return _page(request, db, [f"Delete failed: {exc}"])
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="gpg.key_delete",
        target_type="gpg_key",
        target_id=fingerprint,
    )
    return _page(request, db)
