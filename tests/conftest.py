"""Test fixtures: isolated environment, fresh DB, logged-in web client."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from mirrorhub import models  # noqa: F401
from mirrorhub.db import get_engine
from mirrorhub.security import auth, csrf
from mirrorhub.settings import Settings, get_settings

PASSWORD = "correct-horse-battery"
TOKEN_RE = re.compile(r'X-CSRF-Token": "([0-9a-f]{64})')


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    csrf._app_key.cache_clear()


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("MIRRORHUB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MIRRORHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MIRRORHUB_EXPORT_DIR", str(tmp_path / "export"))
    monkeypatch.setenv("MIRRORHUB_GNUPG_DIR", str(tmp_path / "gnupg"))
    monkeypatch.setenv("MIRRORHUB_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("MIRRORHUB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("MIRRORHUB_GLOBAL_CONCURRENCY", "4")
    _clear_caches()
    auth.ip_limiter.reset()
    settings = get_settings()
    settings.ensure_dirs()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())
    yield settings
    get_engine().dispose()
    _clear_caches()


@pytest.fixture
def client(configured: Settings) -> Iterator[TestClient]:
    from mirrorhub.web.app import create_app

    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    page = client.get("/login")
    match = TOKEN_RE.search(page.text)
    assert match, "csrf token missing from login page"
    response = client.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "csrf_token": match.group(1)},
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def csrf_token(admin: TestClient) -> str:
    match = TOKEN_RE.search(admin.get("/").text)
    assert match, "csrf token missing from dashboard"
    return match.group(1)
