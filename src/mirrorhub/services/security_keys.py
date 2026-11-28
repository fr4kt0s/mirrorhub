"""FIDO2 security keys (WebAuthn) for the admin login.

Login modes, once at least one key is registered (setting ``login_mode``):

- ``either`` (default): password *or* security key. A login with the key alone
  requires user verification, i.e. the key's FIDO2 PIN, so a lost key is not
  enough to log in.
- ``both``: password *and* security key (two-factor). Touching the key suffices.

Without registered keys the password alone logs in, whatever the mode.

The relying party is the host of ``MIRRORHUB_PUBLIC_ORIGIN``: keys are bound to that
host name (not an IP address) and work only over HTTPS with a certificate the
browser trusts. Challenges are single-use, stored as rows (``webauthn_challenge``)
whose id is the hash of a random cookie token, and expire after five minutes.
Keys are not discoverable (no resident keys): the server sends the ids of all
registered keys, which is fine for the single admin.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, col, select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from mirrorhub.models import Admin, SecurityKey, WebAuthnChallenge, utcnow
from mirrorhub.services import appsettings
from mirrorhub.settings import Settings, get_settings

MODE_SETTING = "login_mode"
MODE_EITHER = "either"
MODE_BOTH = "both"
MODES = (MODE_EITHER, MODE_BOTH)

PURPOSE_REGISTER = "register"
PURPOSE_LOGIN = "login"  # security key instead of the password (mode either)
PURPOSE_SECOND = "second"  # security key after the password (mode both)

CHALLENGE_TTL_S = 300
TIMEOUT_MS = 120_000
MAX_NAME = 64
RP_NAME = "MirrorHub"


class SecurityKeyError(Exception):
    """A ceremony that cannot work or failed; the message is shown to the admin."""


# ---- configuration and mode ---------------------------------------------------------


def relying_party(settings: Settings | None = None) -> tuple[str, str]:
    """Return (rp_id, origin) from MIRRORHUB_PUBLIC_ORIGIN, or SecurityKeyError."""
    origin = (settings or get_settings()).public_origin.strip().rstrip("/")
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if not origin or parts.scheme != "https" or not host:
        raise SecurityKeyError(
            "Security keys need MIRRORHUB_PUBLIC_ORIGIN with https:// and a host name"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host, origin
    raise SecurityKeyError(
        "Security keys need a host name in MIRRORHUB_PUBLIC_ORIGIN, not an IP address"
    )


def available(settings: Settings | None = None) -> str | None:
    """Return why security keys cannot be used here, or None if they can."""
    try:
        relying_party(settings)
    except SecurityKeyError as exc:
        return str(exc)
    return None


def all_keys(db: Session) -> list[SecurityKey]:
    """Return every registered key, oldest first."""
    return list(db.exec(select(SecurityKey).order_by(col(SecurityKey.id))).all())


def login_mode(db: Session) -> str:
    """Return the configured login mode (either or both)."""
    value = appsettings.get(db, MODE_SETTING, MODE_EITHER)
    return value if value in MODES else MODE_EITHER


def set_login_mode(db: Session, mode: str) -> None:
    """Store the login mode."""
    if mode not in MODES:
        raise SecurityKeyError("unknown login mode")
    appsettings.set_value(db, MODE_SETTING, mode)


def key_login_allowed(db: Session) -> bool:
    """Return True if the login page offers "Sign in with security key"."""
    return bool(all_keys(db)) and login_mode(db) == MODE_EITHER


def second_factor_required(db: Session) -> bool:
    """Return True if a correct password must be followed by a security key."""
    return bool(all_keys(db)) and login_mode(db) == MODE_BOTH


# ---- challenges ---------------------------------------------------------------------


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_challenge(
    db: Session, purpose: str, challenge: bytes, admin_id: int | None, name: str = ""
) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        WebAuthnChallenge(
            id=_token_id(token),
            purpose=purpose,
            challenge=bytes_to_base64url(challenge),
            admin_id=admin_id,
            name=name,
            expires_at=utcnow() + timedelta(seconds=CHALLENGE_TTL_S),
        )
    )
    db.commit()
    return token


def take_challenge(db: Session, token: str, purposes: tuple[str, ...]) -> WebAuthnChallenge:
    """Consume a pending challenge (single use); SecurityKeyError if absent or expired."""
    row = db.get(WebAuthnChallenge, _token_id(token)) if token else None
    if row is None:
        raise SecurityKeyError("The request expired or was already used; try again")
    db.delete(row)
    db.commit()
    if row.purpose not in purposes or row.expires_at <= utcnow():
        raise SecurityKeyError("The request expired or was already used; try again")
    return row


def purge_challenges(db: Session) -> int:
    """Delete expired challenges; return how many were removed."""
    expired = list(
        db.exec(select(WebAuthnChallenge).where(col(WebAuthnChallenge.expires_at) <= utcnow()))
    )
    for row in expired:
        db.delete(row)
    db.commit()
    return len(expired)


# ---- registration -------------------------------------------------------------------


def _descriptors(keys: list[SecurityKey]) -> list[PublicKeyCredentialDescriptor]:
    out = []
    for key in keys:
        transports = []
        for value in key.transports:
            try:
                transports.append(AuthenticatorTransport(value))
            except ValueError:
                continue
        out.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(key.credential_id), transports=transports or None
            )
        )
    return out


def clean_name(name: str) -> str:
    """Return a usable key name (1-64 printable characters) or SecurityKeyError."""
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise SecurityKeyError(f"Give the key a name of 1-{MAX_NAME} characters")
    return name


def start_registration(db: Session, admin: Admin, name: str) -> tuple[str, str]:
    """Begin registering a key: return (cookie token, options JSON for the browser).

    User verification is required, so the key gets a FIDO2 PIN if it has none yet
    (the browser asks for it); the key can then also log in on its own.
    """
    rp_id, _origin = relying_party()
    name = clean_name(name)
    assert admin.id is not None
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=f"mirrorhub-admin-{admin.id}".encode(),
        user_name=admin.username,
        user_display_name=admin.username,
        timeout=TIMEOUT_MS,
        exclude_credentials=_descriptors(all_keys(db)),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    token = _new_challenge(db, PURPOSE_REGISTER, options.challenge, admin.id, name)
    return token, options_to_json(options)


def _transports(credential: dict[str, Any]) -> list[str]:
    response = credential.get("response")
    values = response.get("transports") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and len(value) <= 16][:8]


def finish_registration_row(
    db: Session, row: WebAuthnChallenge, credential: dict[str, Any]
) -> SecurityKey:
    """Verify the browser's answer for a consumed registration challenge; store the key."""
    rp_id, origin = relying_party()
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(row.challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure, ValueError, KeyError) as exc:
        raise SecurityKeyError(f"The key could not be registered: {exc}") from exc
    credential_id = bytes_to_base64url(verified.credential_id)
    if db.exec(select(SecurityKey).where(SecurityKey.credential_id == credential_id)).first():
        raise SecurityKeyError("This key is already registered")
    assert row.admin_id is not None
    key = SecurityKey(
        admin_id=row.admin_id,
        name=row.name,
        credential_id=credential_id,
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        aaguid=str(verified.aaguid),
        transports=_transports(credential),
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


# ---- login --------------------------------------------------------------------------


def start_login(db: Session, purpose: str, admin_id: int | None = None) -> tuple[str, str]:
    """Begin a key login: return (cookie token, options JSON for the browser).

    PURPOSE_LOGIN (key instead of password) requires the key's PIN; PURPOSE_SECOND
    (key after the password, admin known) only needs a touch.
    """
    rp_id, _origin = relying_party()
    keys = all_keys(db)
    if admin_id is not None:
        keys = [key for key in keys if key.admin_id == admin_id]
    if not keys:
        raise SecurityKeyError("No security key is registered")
    verification = (
        UserVerificationRequirement.REQUIRED
        if purpose == PURPOSE_LOGIN
        else UserVerificationRequirement.DISCOURAGED
    )
    options = generate_authentication_options(
        rp_id=rp_id,
        timeout=TIMEOUT_MS,
        allow_credentials=_descriptors(keys),
        user_verification=verification,
    )
    token = _new_challenge(db, purpose, options.challenge, admin_id)
    return token, options_to_json(options)


def finish_login(
    db: Session, row: WebAuthnChallenge, credential: dict[str, Any]
) -> tuple[Admin, SecurityKey]:
    """Verify a key login for a consumed challenge; return the admin and the key used."""
    rp_id, origin = relying_party()
    credential_id = credential.get("rawId") or credential.get("id")
    if not isinstance(credential_id, str):
        raise SecurityKeyError("The browser sent no key id")
    key = db.exec(select(SecurityKey).where(SecurityKey.credential_id == credential_id)).first()
    if key is None or (row.admin_id is not None and key.admin_id != row.admin_id):
        raise SecurityKeyError("This security key is not registered")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(row.challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(key.public_key),
            credential_current_sign_count=key.sign_count,
            require_user_verification=row.purpose == PURPOSE_LOGIN,
        )
    except (InvalidAuthenticationResponse, InvalidJSONStructure, ValueError, KeyError) as exc:
        raise SecurityKeyError(f"The security key was not accepted: {exc}") from exc
    admin = db.get(Admin, key.admin_id)
    if admin is None:
        raise SecurityKeyError("This security key is not registered")
    key.sign_count = verified.new_sign_count
    key.last_used_at = utcnow()
    db.add(key)
    db.commit()
    return admin, key


# ---- removal ------------------------------------------------------------------------


def remove(db: Session, key_id: int) -> SecurityKey | None:
    """Delete one key; return it, or None if there is no such key."""
    key = db.get(SecurityKey, key_id)
    if key is None:
        return None
    db.delete(key)
    db.commit()
    return key


def remove_all(db: Session) -> int:
    """Delete every key and pending challenge and reset the mode (reset script)."""
    keys = all_keys(db)
    for key in keys:
        db.delete(key)
    for row in db.exec(select(WebAuthnChallenge)).all():
        db.delete(row)
    appsettings.set_value(db, MODE_SETTING, MODE_EITHER, commit=False)
    db.commit()
    return len(keys)
