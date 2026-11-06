"""Login and logout."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.security import auth
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


@router.get("/login")
def login_page(request: Request) -> Response:
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=auth.MAX_PASSWORD_LEN)],
    db: Session = Depends(get_db),
) -> Response:
    ip = client_ip(request)

    if not auth.ip_limiter.allow(ip):
        record(db, actor=username, ip=ip, action="login.rate_limited")
        resp = render(request, "login.html", {"error": "Too many attempts. Wait a minute."}, 429)
        resp.headers["Retry-After"] = "60"
        return resp

    try:
        admin = auth.attempt_login(db, username, password)
    except auth.LoginLocked as exc:
        record(db, actor=username, ip=ip, action="login.locked")
        resp = render(
            request,
            "login.html",
            {"error": f"Account locked. Try again in {exc.retry_after_s} seconds."},
            429,
        )
        resp.headers["Retry-After"] = str(exc.retry_after_s)
        return resp
    except auth.InvalidCredentials:
        record(db, actor=username, ip=ip, action="login.failure")
        return render(request, "login.html", {"error": "Invalid username or password."}, 401)

    token, _sess = auth.create_session(db, admin, ip)
    record(db, actor=admin.username, ip=ip, action="login.success")
    response = RedirectResponse("/", status_code=303)
    set_cookie(response, SESSION_COOKIE, token, get_settings().session_ttl_s)
    response.delete_cookie(PRE_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.post("/logout")
def logout(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    auth.delete_session(db, token)
    record(db, actor=user.admin.username, ip=client_ip(request), action="logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response
