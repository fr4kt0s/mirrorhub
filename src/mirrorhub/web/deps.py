"""Shared database, authentication, CSRF and template dependencies."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from mirrorhub import __version__
from mirrorhub.db import get_engine
from mirrorhub.models import Admin, AuthSession
from mirrorhub.security import auth, csrf

log = logging.getLogger("mirrorhub.security")

SESSION_COOKIE = "__Host-mh_session"
PRE_COOKIE = "__Host-mh_pre"
PRE_COOKIE_TTL_S = 3600

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["ts"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["gb"] = lambda value: f"{(value or 0) / 1e9:.1f}"


def _human_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["bytes"] = _human_bytes

NAV: list[tuple[str, str]] = [
    ("Dashboard", "/"),
    ("Repositories", "/repos"),
    ("Jobs", "/jobs"),
    ("Exports", "/exports"),
    ("Schedules", "/schedules"),
    ("Storage", "/storage"),
    ("Presets", "/presets"),
    ("Keys", "/keys"),
    ("Config", "/config"),
    ("Notifications", "/notifications"),
    ("Settings", "/settings"),
    ("Audit log", "/audit"),
]


@dataclass
class CurrentUser:
    admin: Admin
    session: AuthSession


class NotAuthenticated(Exception):
    """No authenticated admin session is available."""


def get_db() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def set_cookie(
    response: Response,
    key: str,
    value: str,
    max_age: int,
) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


async def security_context(
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    user: CurrentUser | None = None
    secret: str | None = None

    session_token = request.cookies.get(SESSION_COOKIE)
    pre_secret = request.cookies.get(PRE_COOKIE)

    if session_token:
        session = auth.lookup_session(db, session_token)
        if session is not None:
            admin = db.get(Admin, session.admin_id)
            if admin is not None:
                user = CurrentUser(admin=admin, session=session)
                secret = session.csrf_secret

    if secret is None and csrf.valid_secret_format(pre_secret):
        secret = pre_secret

    request.state.user = user
    request.state.csrf_secret = secret

    if request.method in csrf.SAFE_METHODS:
        return

    supplied = await csrf.extract_token(request)
    origin_matches = csrf.origin_ok(request)
    token_matches = False

    if secret is not None and supplied:
        try:
            token_matches = csrf.verify_token(secret, supplied)
        except (TypeError, ValueError):
            token_matches = False

    if secret is None:
        reason = "missing_csrf_context"
    elif not origin_matches:
        reason = "origin_mismatch"
    elif not supplied:
        reason = "missing_request_token"
    elif not token_matches:
        reason = "token_mismatch"
    else:
        return

    log.warning(
        "CSRF rejected: reason=%s session_cookie=%s authenticated=%s "
        "pre_cookie=%s pre_cookie_valid=%s request_token=%s origin_matches=%s",
        reason,
        bool(session_token),
        user is not None,
        bool(pre_secret),
        csrf.valid_secret_format(pre_secret),
        bool(supplied),
        origin_matches,
    )
    raise HTTPException(status_code=403, detail="CSRF validation failed")


def require_user(request: Request) -> CurrentUser:
    user: CurrentUser | None = getattr(request.state, "user", None)
    if user is None:
        raise NotAuthenticated
    return user


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    secret: str | None = getattr(request.state, "csrf_secret", None)
    fresh = secret is None

    if secret is None:
        secret = csrf.new_secret()
        request.state.csrf_secret = secret

    values: dict[str, Any] = {
        "csrf_token": csrf.make_token(secret),
        "user": getattr(request.state, "user", None),
        "nav": NAV,
        "version": __version__,
    }
    values.update(context or {})

    response = templates.TemplateResponse(
        request,
        name,
        values,
        status_code=status_code,
    )
    if fresh:
        set_cookie(response, PRE_COOKIE, secret, PRE_COOKIE_TTL_S)

    return response
