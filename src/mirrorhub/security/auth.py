"""Admin authentication: argon2 hashing, per-(user, IP) lockout, IP rate limiting, sessions."""

import hashlib
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlmodel import Session, col, select

from mirrorhub.models import Admin, AuthSession, LoginThrottle, utcnow
from mirrorhub.services.audit import record
from mirrorhub.settings import Settings, get_settings

MAX_LOCK_S = 3600
MAX_PASSWORD_LEN = 256
# A (username, IP) pair without failures for this long starts again from zero.
THROTTLE_RESET_S = 24 * 3600

# argon2 needs 64 MiB per hash. Concurrent verifications are capped so that a burst
# of login requests (up to 40 threadpool threads) cannot exhaust the container memory.
ARGON2_CONCURRENCY = 4

_hasher = PasswordHasher()
_hash_slots = threading.BoundedSemaphore(ARGON2_CONCURRENCY)
_DUMMY_HASH = _hasher.hash("mirrorhub-dummy-password")


class InvalidCredentials(Exception):
    """Username or password wrong (message intentionally generic)."""


class LoginLocked(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"locked for {retry_after_s}s")
        self.retry_after_s = retry_after_s


class IpRateLimiter:
    """Sliding-window limiter on login attempts per client IP (in-memory, single web instance).

    Memory stays bounded: each address keeps at most ``limit + 1`` timestamps, and
    addresses without hits inside the window are dropped once per window.
    """

    def __init__(
        self,
        limit: int = 10,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._reported: dict[str, float] = {}
        self._last_prune = clock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            hits = self._hits.get(key)
            if hits is None:
                hits = deque(maxlen=self.limit + 1)
                self._hits[key] = hits
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            hits.append(now)
            return len(hits) <= self.limit

    def first_rejection(self, key: str) -> bool:
        """True once per window for a rejected address, so only that one gets audited."""
        with self._lock:
            now = self._clock()
            last = self._reported.get(key)
            if last is not None and now - last <= self.window_s:
                return False
            self._reported[key] = now
            return True

    def _prune(self, now: float) -> None:
        if now - self._last_prune <= self.window_s:
            return
        self._last_prune = now
        self._hits = {
            key: hits
            for key, hits in self._hits.items()
            if hits and now - hits[-1] <= self.window_s
        }
        self._reported = {
            key: ts for key, ts in self._reported.items() if now - ts <= self.window_s
        }

    def tracked(self) -> int:
        """Number of addresses currently held in memory."""
        with self._lock:
            return len(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._reported.clear()


ip_limiter = IpRateLimiter()


def hash_password(password: str) -> str:
    with _hash_slots:
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        with _hash_slots:
            return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def bootstrap_admin(db: Session, settings: Settings | None = None) -> str | None:
    """Create the admin on first start. Returns the generated password, or None if not generated."""
    s = settings or get_settings()
    if db.exec(select(Admin)).first() is not None:
        return None
    password = s.admin_password
    generated = False
    if not password:
        password = secrets.token_urlsafe(18)
        generated = True
    db.add(Admin(username=s.admin_username, password_hash=hash_password(password)))
    record(
        db,
        actor="system",
        action="admin.bootstrap",
        target_type="admin",
        target_id=s.admin_username,
        detail={"generated_password": generated},
        commit=False,
    )
    db.commit()
    return password if generated else None


def print_generated_password(username: str, password: str) -> None:
    """Printed once straight to stderr (not via logging, whose filter redacts passwords)."""
    banner = "=" * 64
    sys.stderr.write(
        f"\n{banner}\nMirrorHub first start: initial admin credentials (shown only once)\n"
        f"  username: {username}\n  password: {password}\n{banner}\n\n"
    )
    sys.stderr.flush()


def _throttle_key(username: str, ip: str) -> str:
    return hashlib.sha256(f"{username}\0{ip}".encode()).hexdigest()


def _throttle(db: Session, username: str, ip: str, now: datetime) -> LoginThrottle:
    key = _throttle_key(username, ip)
    row = db.get(LoginThrottle, key)
    if row is None:
        return LoginThrottle(key=key, username=username[:64], ip=ip[:64], updated_at=now)
    unlocked = row.locked_until is None or row.locked_until <= now
    if unlocked and now - row.updated_at > timedelta(seconds=THROTTLE_RESET_S):
        row.failed_attempts = 0
        row.locked_until = None
    return row


def attempt_login(
    db: Session,
    username: str,
    password: str,
    ip: str = "",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Admin:
    """Verify credentials with lockout per (username, client IP).

    Failures from one address lock only that address for this username, with
    exponential backoff capped at MAX_LOCK_S. The admin can still log in from any
    other address, so a remote attacker cannot lock the account out. Unknown
    usernames are throttled the same way, which keeps responses uniform.
    """
    s = settings or get_settings()
    now = now or utcnow()
    password = password[:MAX_PASSWORD_LEN]

    throttle = _throttle(db, username, ip, now)
    if throttle.locked_until is not None and throttle.locked_until > now:
        raise LoginLocked(int((throttle.locked_until - now).total_seconds()) + 1)

    admin = db.exec(select(Admin).where(Admin.username == username)).first()
    if admin is None:
        verify_password(_DUMMY_HASH, password)  # equalize timing
        valid = False
    else:
        valid = verify_password(admin.password_hash, password)

    if not valid:
        throttle.failed_attempts += 1
        throttle.updated_at = now
        lock_s = 0
        if throttle.failed_attempts >= s.login_max_attempts:
            exponent = min(throttle.failed_attempts - s.login_max_attempts, 16)
            lock_s = min(s.login_lockout_s * 2**exponent, MAX_LOCK_S)
            throttle.locked_until = now + timedelta(seconds=lock_s)
        db.add(throttle)
        db.commit()
        if lock_s:
            raise LoginLocked(lock_s)
        raise InvalidCredentials

    assert admin is not None
    if _hasher.check_needs_rehash(admin.password_hash):
        admin.password_hash = hash_password(password)
        db.add(admin)
    existing = db.get(LoginThrottle, throttle.key)
    if existing is not None:
        db.delete(existing)
    db.commit()
    return admin


def purge_stale_throttles(db: Session, now: datetime | None = None) -> int:
    """Delete unlocked throttle rows that have been quiet for THROTTLE_RESET_S."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=THROTTLE_RESET_S)
    rows = db.exec(select(LoginThrottle).where(col(LoginThrottle.updated_at) < cutoff)).all()
    stale = [row for row in rows if row.locked_until is None or row.locked_until <= now]
    for row in stale:
        db.delete(row)
    db.commit()
    return len(stale)


def _session_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    db: Session, admin: Admin, ip: str, settings: Settings | None = None
) -> tuple[str, AuthSession]:
    """Returns (cookie token, session row). Only the token's hash is stored."""
    s = settings or get_settings()
    assert admin.id is not None
    token = secrets.token_urlsafe(32)
    now = utcnow()
    row = AuthSession(
        id=_session_id(token),
        admin_id=admin.id,
        csrf_secret=secrets.token_urlsafe(32),
        ip=ip[:64],
        created_at=now,
        expires_at=now + timedelta(seconds=s.session_ttl_s),
    )
    db.add(row)
    db.commit()
    return token, row


def lookup_session(db: Session, token: str, now: datetime | None = None) -> AuthSession | None:
    if not token or len(token) > 128:
        return None
    row = db.get(AuthSession, _session_id(token))
    if row is None or row.expires_at <= (now or utcnow()):
        return None
    return row


def delete_session(db: Session, token: str) -> None:
    row = db.get(AuthSession, _session_id(token))
    if row is not None:
        db.delete(row)
        db.commit()


def purge_expired_sessions(db: Session) -> int:
    rows = db.exec(select(AuthSession).where(col(AuthSession.expires_at) <= utcnow())).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)
