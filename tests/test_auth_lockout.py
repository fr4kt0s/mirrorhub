"""Login lockout per (username, client IP)."""

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Admin, LoginThrottle, utcnow
from mirrorhub.security import auth
from mirrorhub.settings import Settings

PASSWORD = "correct-horse-battery"  # same as conftest.PASSWORD
ATTACKER = "203.0.113.7"
ADMIN_IP = "198.51.100.20"


@pytest.fixture
def db(configured: Settings) -> Iterator[Session]:
    with Session(get_engine()) as session:
        auth.bootstrap_admin(session, configured)
        yield session


def _fail(
    db: Session, settings: Settings, ip: str, times: int, now: datetime | None = None
) -> None:
    for _ in range(times):
        with pytest.raises((auth.InvalidCredentials, auth.LoginLocked)):
            auth.attempt_login(db, settings.admin_username, "wrong", ip, settings, now)


def test_failures_lock_only_the_offending_address(db: Session, configured: Settings) -> None:
    _fail(db, configured, ATTACKER, configured.login_max_attempts)

    with pytest.raises(auth.LoginLocked):
        auth.attempt_login(db, configured.admin_username, PASSWORD, ATTACKER, configured)

    admin = auth.attempt_login(db, configured.admin_username, PASSWORD, ADMIN_IP, configured)
    assert admin.username == configured.admin_username


def test_lockout_starts_at_max_attempts_and_escalates(db: Session, configured: Settings) -> None:
    now = utcnow()
    _fail(db, configured, ATTACKER, configured.login_max_attempts - 1, now)

    with pytest.raises(auth.LoginLocked) as first:
        auth.attempt_login(db, configured.admin_username, "wrong", ATTACKER, configured, now)
    assert first.value.retry_after_s == configured.login_lockout_s

    later = now + timedelta(seconds=configured.login_lockout_s + 1)
    with pytest.raises(auth.LoginLocked) as second:
        auth.attempt_login(db, configured.admin_username, "wrong", ATTACKER, configured, later)
    assert second.value.retry_after_s == min(2 * configured.login_lockout_s, auth.MAX_LOCK_S)


def test_lock_is_capped(db: Session, configured: Settings) -> None:
    now = utcnow()
    for _ in range(configured.login_max_attempts + 20):
        with pytest.raises((auth.InvalidCredentials, auth.LoginLocked)):
            auth.attempt_login(db, configured.admin_username, "wrong", ATTACKER, configured, now)
        row = db.exec(select(LoginThrottle)).one()
        if row.locked_until is not None:
            assert row.locked_until - now <= timedelta(seconds=auth.MAX_LOCK_S)
            now = row.locked_until + timedelta(seconds=1)


def test_success_clears_the_pair_counter(db: Session, configured: Settings) -> None:
    _fail(db, configured, ADMIN_IP, configured.login_max_attempts - 1)
    auth.attempt_login(db, configured.admin_username, PASSWORD, ADMIN_IP, configured)

    assert db.exec(select(LoginThrottle)).all() == []
    admin = db.exec(select(Admin)).one()
    assert admin.failed_attempts == 0
    assert admin.locked_until is None


def test_counter_resets_after_quiet_period(db: Session, configured: Settings) -> None:
    now = utcnow()
    _fail(db, configured, ADMIN_IP, configured.login_max_attempts - 1, now)

    later = now + timedelta(seconds=auth.THROTTLE_RESET_S + 1)
    with pytest.raises(auth.InvalidCredentials):
        auth.attempt_login(db, configured.admin_username, "wrong", ADMIN_IP, configured, later)
    row = db.exec(select(LoginThrottle)).one()
    assert row.failed_attempts == 1


def test_unknown_usernames_are_throttled_too(db: Session, configured: Settings) -> None:
    for _ in range(configured.login_max_attempts - 1):
        with pytest.raises(auth.InvalidCredentials):
            auth.attempt_login(db, "nobody", "wrong", ATTACKER, configured)
    with pytest.raises(auth.LoginLocked):
        auth.attempt_login(db, "nobody", "wrong", ATTACKER, configured)


def test_purge_removes_only_stale_unlocked_rows(db: Session, configured: Settings) -> None:
    now = utcnow()
    _fail(db, configured, ADMIN_IP, 1, now - timedelta(seconds=auth.THROTTLE_RESET_S + 10))
    _fail(db, configured, ATTACKER, configured.login_max_attempts, now)

    assert auth.purge_stale_throttles(db, now) == 1
    remaining = db.exec(select(LoginThrottle)).all()
    assert [row.ip for row in remaining] == [ATTACKER]
