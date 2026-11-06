"""Auth, lockout, CSRF and security header tests (no subprocess involved)."""

import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog

PASSWORD = "correct-horse-battery"
TOKEN_RE = re.compile(r'X-CSRF-Token": "([0-9a-f]{64})')


def _token(html: str) -> str:
    match = TOKEN_RE.search(html)
    assert match, "csrf token not found in page"
    return match.group(1)


def _login(client: TestClient, password: str = PASSWORD) -> int:
    page = client.get("/login")
    resp = client.post(
        "/login",
        data={"username": "admin", "password": password, "csrf_token": _token(page.text)},
    )
    return resp.status_code


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_without_csrf_is_rejected(client: TestClient) -> None:
    client.get("/login")
    resp = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 403


def test_login_success_sets_hardened_cookie(client: TestClient) -> None:
    page = client.get("/login")
    resp = client.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "csrf_token": _token(page.text)},
    )
    assert resp.status_code == 303
    cookie = next(
        c for c in resp.headers.get_list("set-cookie") if c.startswith("__Host-mh_session=")
    ).lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/").status_code == 200


def test_lockout_blocks_correct_password(client: TestClient) -> None:
    codes = [_login(client, "wrong-password") for _ in range(5)]
    assert codes[:4] == [401, 401, 401, 401]
    assert codes[4] == 429
    assert _login(client) == 429


def test_logout_requires_csrf(client: TestClient) -> None:
    assert _login(client) == 303
    assert client.post("/logout").status_code == 403
    token = _token(client.get("/").text)
    resp = client.post("/logout", headers={"X-CSRF-Token": token})
    assert resp.status_code == 303
    assert client.get("/").status_code == 303


def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/login")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert "unsafe-inline" not in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"


def test_login_events_are_audited(client: TestClient) -> None:
    _login(client, "wrong-password")
    _login(client)
    with Session(get_engine()) as db:
        actions = [r.action for r in db.exec(select(AuditLog)).all()]
    assert "admin.bootstrap" in actions
    assert "login.failure" in actions
    assert "login.success" in actions
