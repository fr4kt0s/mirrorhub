"""Web GUI review fixes: pre-auth limits, secret key, presets, templates."""

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Admin, AuditLog, Job, Preset, Repository
from mirrorhub.security import auth, csrf
from mirrorhub.services import presets
from mirrorhub.settings import Settings, get_secret_key
from mirrorhub.web import sse

TOKEN_RE = re.compile(r'X-CSRF-Token": "([0-9a-f]{64})')
TEMPLATES = Path(__file__).resolve().parents[1] / "src/mirrorhub/web/templates"


def _token(html: str) -> str:
    match = TOKEN_RE.search(html)
    assert match, "csrf token not found in page"
    return match.group(1)


# --- IP rate limiter -------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_limiter_keeps_memory_bounded() -> None:
    clock = _Clock()
    limiter = auth.IpRateLimiter(limit=3, window_s=60, clock=clock)
    for _ in range(1000):
        limiter.allow("198.51.100.7")
    assert len(limiter._hits["198.51.100.7"]) == 4  # limit + 1

    for index in range(500):
        limiter.allow(f"203.0.113.{index}")
    assert limiter.tracked() == 501

    clock.now = 200.0  # every address quiet for more than one window
    limiter.allow("192.0.2.1")
    assert limiter.tracked() == 1


def test_limiter_keeps_rejecting_while_hammered_and_recovers() -> None:
    clock = _Clock()
    limiter = auth.IpRateLimiter(limit=3, window_s=60, clock=clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    for _ in range(100):
        clock.now += 1
        assert not limiter.allow("a")
    clock.now += 61
    assert limiter.allow("a")


def test_first_rejection_is_reported_once_per_window() -> None:
    clock = _Clock()
    limiter = auth.IpRateLimiter(limit=1, window_s=60, clock=clock)
    assert limiter.first_rejection("a")
    assert not limiter.first_rejection("a")
    assert limiter.first_rejection("b")
    clock.now = 61
    assert limiter.first_rejection("a")


def test_rate_limited_logins_are_audited_once(client: TestClient) -> None:
    token = _token(client.get("/login").text)
    codes = [
        client.post(
            "/login", data={"username": "admin", "password": "wrong", "csrf_token": token}
        ).status_code
        for _ in range(25)
    ]
    assert codes[-1] == 429
    with Session(get_engine()) as db:
        actions = [row.action for row in db.exec(select(AuditLog)).all()]
    assert actions.count("login.rate_limited") == 1
    assert actions.count("login.failure") + actions.count("login.locked") == auth.ip_limiter.limit


def test_argon2_runs_at_most_n_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowHasher:
        def verify(self, _hash: str, _password: str) -> bool:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.05)
            with lock:
                active -= 1
            return False

    monkeypatch.setattr(auth, "_hasher", SlowHasher())
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: auth.verify_password("h", "p"), range(16)))
    assert 1 <= peak <= auth.ARGON2_CONCURRENCY


# --- CSRF check before the body is read --------------------------------------------


def test_post_without_csrf_context_does_not_read_the_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def must_not_read(_request: object) -> str:
        raise AssertionError("form body read before the CSRF context check")

    monkeypatch.setattr(csrf, "extract_token", must_not_read)
    response = client.post("/keys/upload", files={"keyfile": ("big.asc", b"x" * 4096)})
    assert response.status_code == 403


def test_post_from_foreign_origin_does_not_read_the_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/login")  # sets the pre-auth CSRF cookie

    async def must_not_read(_request: object) -> str:
        raise AssertionError("form body read for a foreign origin")

    monkeypatch.setattr(csrf, "extract_token", must_not_read)
    response = client.post(
        "/keys/upload",
        files={"keyfile": ("big.asc", b"x" * 4096)},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403


# --- Secret key ---------------------------------------------------------------------


def test_generated_secret_key_is_shared_by_concurrent_callers(
    configured: Settings,
) -> None:
    settings = configured.model_copy(update={"secret_key": ""})
    barrier = threading.Barrier(8)

    def read_key(_: int) -> str:
        barrier.wait(timeout=10)
        return get_secret_key(settings)

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = set(pool.map(read_key, range(8)))
    assert len(keys) == 1
    assert len(keys.pop()) >= 32
    assert list(settings.state_dir.glob(".secret_key.*")) == []
    assert (settings.state_dir / "secret_key").stat().st_mode & 0o777 == 0o600


def test_empty_secret_key_file_is_an_error(configured: Settings) -> None:
    settings = configured.model_copy(update={"secret_key": ""})
    (settings.state_dir / "secret_key").write_text("\n")
    with pytest.raises(RuntimeError, match="is empty"):
        get_secret_key(settings)


# --- Presets ------------------------------------------------------------------------


def _seed_names() -> set[str]:
    with Session(get_engine()) as db:
        return {row.name for row in db.exec(select(Preset).where(Preset.is_seed)).all()}


def test_deleted_seed_preset_stays_deleted(admin: TestClient, csrf_token: str) -> None:
    name = presets.SEED_PRESETS[0]["name"]
    with Session(get_engine()) as db:
        row = db.exec(select(Preset).where(Preset.name == name)).one()
        preset_id = row.id

    response = admin.post(f"/presets/{preset_id}/delete", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 303
    assert name not in admin.get("/presets").text
    with Session(get_engine()) as db:
        assert presets.seed(db) == 0  # e.g. on the next start
    assert name not in _seed_names()

    response = admin.post("/presets/reseed", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 303
    assert name in _seed_names()
    with Session(get_engine()) as db:
        actions = [row.action for row in db.exec(select(AuditLog)).all()]
    assert "preset.reseed" in actions


# --- Templates ----------------------------------------------------------------------


def test_every_htmx_post_has_a_target() -> None:
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text()
        for match in re.finditer(r"<[^>]*hx-post=[^>]*>", text):
            assert "hx-target=" in match.group(0), f"{path.name}: {match.group(0)}"


def test_job_filter_is_not_echoed_into_hx_vals(admin: TestClient) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="r", name="r")
        db.add(repo)
        db.flush()
        assert repo.id is not None
        db.add(Job(repo_id=repo.id, tool="debmirror", status="succeeded"))
        db.commit()

    page = admin.get("/jobs", params={"status": 'x", "injected": "1'})
    assert page.status_code == 200
    assert "injected" not in page.text

    page = admin.get("/jobs", params={"status": "succeeded"})
    assert "hx-vals='{&#34;status&#34;" not in page.text
    assert '"status": "succeeded"' in page.text


# --- SSE ------------------------------------------------------------------------


class _OpenRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _first_event(job_id: int, token: str) -> str:
    async def run() -> str:
        stream = sse.job_events(job_id, _OpenRequest(), 0, token)  # type: ignore[arg-type]
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    return asyncio.run(run())


def test_sse_stream_ends_when_the_session_ends(configured: Settings) -> None:
    with Session(get_engine()) as db:
        auth.bootstrap_admin(db, configured)
        admin = db.exec(select(Admin)).one()
        token, _row = auth.create_session(db, admin, "192.0.2.1", configured)
        repo = Repository(slug="sse", name="sse")
        db.add(repo)
        db.flush()
        assert repo.id is not None
        job = Job(repo_id=repo.id, tool="debmirror", status="running")
        db.add(job)
        db.commit()
        assert job.id is not None
        job_id = job.id

    assert _first_event(job_id, token).startswith("event: status")

    with Session(get_engine()) as db:
        auth.delete_session(db, token)
    assert _first_event(job_id, token).startswith("event: logout")
    assert _first_event(job_id, "").startswith("event: logout")
