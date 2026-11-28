"""Login (password, security key, or both) and logout."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from mirrorhub.models import Admin
from mirrorhub.security import auth
from mirrorhub.services import security_keys
from mirrorhub.services.audit import record
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import (
    PRE_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    client_ip,
    get_db,
    render,
    require_user,
    set_cookie,
)

router = APIRouter()

# token of a pending WebAuthn challenge (security key login), see services/security_keys
WEBAUTHN_COOKIE = "__Host-mh_wa"


def _login_page(
    request: Request, db: Session, error: str | None = None, status_code: int = 200
) -> Response:
    context: dict[str, Any] = {"key_login": security_keys.key_login_allowed(db)}
    if error:
        context["error"] = error
    return render(request, "login.html", context, status_code)


def _clear(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")


def _start_session(request: Request, db: Session, admin: Admin, ip: str) -> str:
    token, _sess = auth.create_session(db, admin, ip)
    return token


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Show the login form (redirect to the dashboard if already logged in)."""
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=303)
    return _login_page(request, db)


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=auth.MAX_PASSWORD_LEN)],
    db: Session = Depends(get_db),
) -> Response:
    """Check the credentials (with per-user and per-IP throttling) and start a session."""
    ip = client_ip(request)

    if not auth.ip_limiter.allow(ip):
        # Rejected requests come without any password check; audit only the first one
        # per address and window so that an unauthenticated client cannot fill the
        # append-only audit log.
        if auth.ip_limiter.first_rejection(ip):
            record(db, actor=username, ip=ip, action="login.rate_limited")
        resp = _login_page(request, db, "Too many attempts. Wait a minute.", 429)
        resp.headers["Retry-After"] = "60"
        return resp

    try:
        admin = auth.attempt_login(db, username, password, ip)
    except auth.LoginLocked as exc:
        record(db, actor=username, ip=ip, action="login.locked")
        resp = _login_page(
            request,
            db,
            "Too many failed attempts from this address. "
            f"Try again in {exc.retry_after_s} seconds.",
            429,
        )
        resp.headers["Retry-After"] = str(exc.retry_after_s)
        return resp
    except auth.InvalidCredentials:
        record(db, actor=username, ip=ip, action="login.failure")
        return _login_page(request, db, "Invalid username or password.", 401)

    if security_keys.second_factor_required(db):
        # Password and security key: the session starts only after the key (/login/key).
        assert admin.id is not None
        try:
            challenge, options = security_keys.start_login(
                db, security_keys.PURPOSE_SECOND, admin.id
            )
        except security_keys.SecurityKeyError as exc:
            record(db, actor=admin.username, ip=ip, action="login.second_factor_unavailable")
            return _login_page(
                request,
                db,
                f"{exc}. A security key is required but cannot be used; on the host run "
                "scripts/reset_password.sh --security-keys-only.",
                503,
            )
        record(db, actor=admin.username, ip=ip, action="login.password_ok")
        page = render(request, "login_key.html", {"options": options})
        set_cookie(page, WEBAUTHN_COOKIE, challenge, security_keys.CHALLENGE_TTL_S)
        return page

    token = _start_session(request, db, admin, ip)
    record(db, actor=admin.username, ip=ip, action="login.success", detail={"method": "password"})
    response = RedirectResponse("/", status_code=303)
    set_cookie(response, SESSION_COOKIE, token, get_settings().session_ttl_s)
    response.delete_cookie(PRE_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@router.post("/login/key/options")
def key_options(request: Request, db: Session = Depends(get_db)) -> Response:
    """Start a login with a security key instead of the password (mode "either")."""
    ip = client_ip(request)
    if not auth.ip_limiter.allow(ip):
        return _error("Too many attempts. Wait a minute.", 429)
    if not security_keys.key_login_allowed(db):
        return _error("Signing in with a security key is not enabled")
    try:
        challenge, options = security_keys.start_login(db, security_keys.PURPOSE_LOGIN)
    except security_keys.SecurityKeyError as exc:
        return _error(str(exc))
    response = Response(content=f'{{"options": {options}}}', media_type="application/json")
    set_cookie(response, WEBAUTHN_COOKIE, challenge, security_keys.CHALLENGE_TTL_S)
    return response


@router.post("/login/key")
async def key_login(request: Request, db: Session = Depends(get_db)) -> Response:
    """Finish a security key login (instead of or after the password) and start a session.

    Failures count towards the same per-(user, address) lockout as wrong passwords.
    The per-address rate limit applies to /login/key/options (and the password form):
    every attempt here needs a fresh single-use challenge from there.
    """
    ip = client_ip(request)
    try:
        body = await request.json()
    except ValueError:
        return _error("invalid request")
    credential = body.get("credential") if isinstance(body, dict) else None
    if not isinstance(credential, dict):
        return _error("invalid request")

    account = db.exec(select(Admin)).first()
    username = account.username if account is not None else "admin"
    try:
        throttle = auth.check_lock(db, username, ip)
    except auth.LoginLocked as exc:
        record(db, actor=username, ip=ip, action="login.locked")
        return _error(f"Too many failed attempts. Try again in {exc.retry_after_s} seconds.", 429)

    purpose = ""
    try:
        row = security_keys.take_challenge(
            db,
            request.cookies.get(WEBAUTHN_COOKIE, ""),
            (security_keys.PURPOSE_LOGIN, security_keys.PURPOSE_SECOND),
        )
        purpose = row.purpose
        if purpose == security_keys.PURPOSE_LOGIN and not security_keys.key_login_allowed(db):
            raise security_keys.SecurityKeyError("Signing in with a security key is not enabled")
        admin, key = security_keys.finish_login(db, row, credential)
    except security_keys.SecurityKeyError as exc:
        record(
            db,
            actor=username,
            ip=ip,
            action="login.key_failure",
            detail={"step": purpose or "unknown", "error": str(exc)[:200]},
        )
        failed = _error(str(exc), 401)
        try:
            auth.register_failure(db, throttle)
        except auth.LoginLocked as locked:
            failed = _error(
                f"Too many failed attempts. Try again in {locked.retry_after_s} seconds.", 429
            )
        _clear(failed, WEBAUTHN_COOKIE)
        return failed

    auth.clear_failures(db, throttle)
    token = _start_session(request, db, admin, ip)
    method = (
        "password + security key" if purpose == security_keys.PURPOSE_SECOND else "security key"
    )
    record(
        db,
        actor=admin.username,
        ip=ip,
        action="login.success",
        detail={"method": method, "key": key.name},
    )
    response = JSONResponse({"redirect": "/"})
    set_cookie(response, SESSION_COOKIE, token, get_settings().session_ttl_s)
    _clear(response, WEBAUTHN_COOKIE)
    _clear(response, PRE_COOKIE)
    return response


@router.post("/logout")
def logout(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """End the session and clear its cookie."""
    token = request.cookies.get(SESSION_COOKIE, "")
    auth.delete_session(db, token)
    record(db, actor=user.admin.username, ip=client_ip(request), action="logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response
