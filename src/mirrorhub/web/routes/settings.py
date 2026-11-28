"""Runtime settings page (log retention, disk thresholds, security keys, login mode)."""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from mirrorhub.security import auth
from mirrorhub.services import appsettings, audit, security_keys
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import (
    CurrentUser,
    client_ip,
    get_db,
    render,
    require_user,
    set_cookie,
)

router = APIRouter()

# token of a pending key registration (see services/security_keys)
REGISTER_COOKIE = "__Host-mh_wa_reg"


def _context(db: Session, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "values": appsettings.snapshot(db),
        "env": get_settings(),
        "errors": errors or [],
        "keys": security_keys.all_keys(db),
        "login_mode": security_keys.login_mode(db),
        "keys_unavailable": security_keys.available(),
    }


@router.get("/settings")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Show the settings page."""
    return render(request, "settings.html", _context(db))


@router.post("/settings")
async def save(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Save log retention and disk alert thresholds."""
    form = await request.form()
    errors: list[str] = []
    try:
        retention = int(str(form.get("log_retention_days") or ""))
        warn = float(str(form.get("disk_warn_percent") or ""))
        critical = float(str(form.get("disk_critical_percent") or ""))
    except ValueError:
        return render(request, "settings.html", _context(db, ["numbers only"]), 400)
    if not 1 <= retention <= 3650:
        errors.append("log retention must be 1-3650 days")
    if not 1 <= warn < critical <= 100:
        errors.append("disk thresholds must satisfy 1 <= warn < critical <= 100")
    if errors:
        return render(request, "settings.html", _context(db, errors), 400)

    appsettings.set_value(db, "log_retention_days", retention, commit=False)
    appsettings.set_value(db, "disk_warn_percent", warn, commit=False)
    appsettings.set_value(db, "disk_critical_percent", critical, commit=False)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="settings.update",
        target_type="setting",
        target_id="runtime",
        detail={
            "log_retention_days": retention,
            "disk_warn_percent": warn,
            "disk_critical_percent": critical,
        },
        commit=False,
    )
    db.commit()
    return RedirectResponse("/settings", status_code=303)


def _confirm_password(db: Session, user: CurrentUser, password: str, ip: str) -> str | None:
    """Check the admin's password again; return an error message or None.

    Wrong passwords count towards the login lockout of (user, address).
    """
    username = user.admin.username
    try:
        throttle = auth.check_lock(db, username, ip)
    except auth.LoginLocked as exc:
        return f"Too many failed attempts. Try again in {exc.retry_after_s} seconds."
    if auth.verify_password(user.admin.password_hash, password[: auth.MAX_PASSWORD_LEN]):
        auth.clear_failures(db, throttle)
        return None
    audit.record(db, actor=username, ip=ip, action="settings.password_confirm_failure")
    try:
        auth.register_failure(db, throttle)
    except auth.LoginLocked as exc:
        return f"Too many failed attempts. Try again in {exc.retry_after_s} seconds."
    return "Wrong password"


@router.post("/settings/security-keys/options")
async def key_register_options(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Start registering a security key (JSON: name, password)."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    error = _confirm_password(db, user, str(body.get("password") or ""), client_ip(request))
    if error:
        return JSONResponse({"error": error}, status_code=403)
    try:
        token, options = security_keys.start_registration(
            db, user.admin, str(body.get("name") or "")
        )
    except security_keys.SecurityKeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    response = Response(content=f'{{"options": {options}}}', media_type="application/json")
    set_cookie(response, REGISTER_COOKIE, token, security_keys.CHALLENGE_TTL_S)
    return response


@router.post("/settings/security-keys")
async def key_register(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Finish registering a security key (JSON: credential from the browser)."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    credential = body.get("credential") if isinstance(body, dict) else None
    if not isinstance(credential, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    try:
        row = security_keys.take_challenge(
            db, request.cookies.get(REGISTER_COOKIE, ""), (security_keys.PURPOSE_REGISTER,)
        )
        if row.admin_id != user.admin.id:
            raise security_keys.SecurityKeyError("The request belongs to another session")
        key = security_keys.finish_registration_row(db, row, credential)
    except security_keys.SecurityKeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="security_key.register",
        target_type="security_key",
        target_id=str(key.id),
        detail={"name": key.name, "aaguid": key.aaguid, "transports": key.transports},
    )
    response = JSONResponse({"redirect": "/settings#security-keys"})
    response.delete_cookie(REGISTER_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.post("/settings/security-keys/{key_id}/delete")
async def key_remove(
    key_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a security key (form: password)."""
    form = await request.form()
    ip = client_ip(request)
    error = _confirm_password(db, user, str(form.get("password") or ""), ip)
    if error:
        return render(request, "settings.html", _context(db, [error]), 403)
    key = security_keys.remove(db, key_id)
    if key is None:
        return render(request, "settings.html", _context(db, ["No such security key"]), 404)
    audit.record(
        db,
        actor=user.admin.username,
        ip=ip,
        action="security_key.remove",
        target_type="security_key",
        target_id=str(key_id),
        detail={"name": key.name, "left": len(security_keys.all_keys(db))},
    )
    return RedirectResponse("/settings#security-keys", status_code=303)


@router.post("/settings/login-mode")
async def save_login_mode(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Switch between "password or security key" and "password and security key"."""
    form = await request.form()
    ip = client_ip(request)
    mode = str(form.get("mode") or "")
    if mode not in security_keys.MODES:
        return render(request, "settings.html", _context(db, ["Unknown login mode"]), 400)
    error = _confirm_password(db, user, str(form.get("password") or ""), ip)
    if error:
        return render(request, "settings.html", _context(db, [error]), 403)
    before = security_keys.login_mode(db)
    security_keys.set_login_mode(db, mode)
    audit.record(
        db,
        actor=user.admin.username,
        ip=ip,
        action="settings.login_mode",
        target_type="setting",
        target_id=security_keys.MODE_SETTING,
        detail={"from": before, "to": mode},
    )
    return RedirectResponse("/settings#security-keys", status_code=303)
