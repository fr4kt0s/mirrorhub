"""CSRF tokens and exact source-origin validation."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import Request

from mirrorhub.settings import get_secret_key, get_settings

log = logging.getLogger("mirrorhub.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HEADER_NAME = "X-CSRF-Token"
FIELD_NAME = "csrf_token"

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

Origin = tuple[str, str, int]


@lru_cache(maxsize=1)
def _app_key() -> str:
    return get_secret_key()


def new_secret() -> str:
    """Return a new random CSRF secret (stored in the session or a pre-login cookie)."""
    return secrets.token_urlsafe(32)


def valid_secret_format(value: str | None) -> bool:
    """Return True if value looks like a secret made by new_secret()."""
    return bool(value and _SECRET_RE.fullmatch(value))


def make_token(secret: str, key: str | None = None) -> str:
    """Return the CSRF token for a secret (HMAC-SHA256 with the application key)."""
    signing_key = key if key is not None else _app_key()
    return hmac.new(
        signing_key.encode(),
        secret.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_token(
    secret: str,
    token: str,
    key: str | None = None,
) -> bool:
    """Return True if token is the valid CSRF token for secret (constant-time compare)."""
    if not _TOKEN_RE.fullmatch(token):
        return False
    return hmac.compare_digest(make_token(secret, key), token)


def _parse_origin(value: str) -> Origin | None:
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        return None

    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
        ):
            return None

        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        if not 1 <= port <= 65535:
            return None
    except ValueError:
        return None

    return parsed.scheme.lower(), parsed.hostname.lower(), port


def origin_ok(request: Request) -> bool:
    """Return True if the request's Origin header names this site.

    Compared with MIRRORHUB_PUBLIC_ORIGIN, or the request's own host if that is not
    set. A missing Origin passes; the CSRF token is checked separately.
    """
    supplied = request.headers.get("origin")

    # Missing Origin is permitted only alongside the separate mandatory
    # CSRF-token validation in security_context.
    if supplied is None:
        return True

    source = _parse_origin(supplied)
    if source is None:
        return False

    configured = get_settings().public_origin
    if configured:
        target = _parse_origin(configured)
        if target is None or target[0] != "https":
            log.error("MIRRORHUB_PUBLIC_ORIGIN is not a valid HTTPS origin")
            return False
    else:
        # Compatibility for isolated tests and direct deployments.
        # Proxy deployments should configure MIRRORHUB_PUBLIC_ORIGIN.
        host = request.headers.get("host", "")
        target = _parse_origin(f"{request.url.scheme}://{host}")
        if target is None:
            return False

    return source == target


async def extract_token(request: Request) -> str:
    """Return the CSRF token from the request header or the form field ("" if none)."""
    header = request.headers.get(HEADER_NAME)
    if header:
        return header

    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(FIELD_NAME)
        if isinstance(value, str):
            return value

    return ""
