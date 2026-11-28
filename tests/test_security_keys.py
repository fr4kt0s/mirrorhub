"""Security keys (FIDO2/WebAuthn): registration, login modes, lockout, removal, reset.

A software authenticator (ES256, attestation "none") stands in for the YubiKey.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import PASSWORD, TOKEN_RE
from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog, SecurityKey
from mirrorhub.services import security_keys
from mirrorhub.settings import Settings

ORIGIN = "https://testserver"
OPTIONS_RE = re.compile(r'data-options="([^"]+)"')


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftKey:
    """A minimal FIDO2 authenticator for tests."""

    def __init__(self, rp_id: str = "testserver") -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.rp_id = rp_id
        self.count = 0

    def _cose(self) -> bytes:
        numbers = self.private.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, flags: int, attested: bool = False) -> bytes:
        self.count += 1
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
        data += self.count.to_bytes(4, "big")
        if attested:
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id
            data += self._cose()
        return data

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict[str, Any], *, uv: bool = True) -> dict[str, Any]:
        client_data = self._client_data("webauthn.create", options["challenge"], ORIGIN)
        auth_data = self._auth_data(0x01 | 0x40 | (0x04 if uv else 0), attested=True)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(attestation),
                "transports": ["usb"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self, options: dict[str, Any], *, uv: bool = True, origin: str = ORIGIN
    ) -> dict[str, Any]:
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = self._auth_data(0x01 | (0x04 if uv else 0))
        signature = self.private.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


@pytest.fixture(autouse=True)
def public_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRRORHUB_PUBLIC_ORIGIN", ORIGIN)


def _token(client: TestClient, path: str = "/") -> str:
    match = TOKEN_RE.search(client.get(path).text)
    assert match
    return match.group(1)


def _post(client: TestClient, path: str, body: dict[str, Any], token: str) -> Any:
    return client.post(path, json=body, headers={"X-CSRF-Token": token, "Origin": ORIGIN})


def _register(client: TestClient, key: SoftKey, name: str = "YubiKey") -> Any:
    token = _token(client, "/settings")
    started = _post(
        client, "/settings/security-keys/options", {"name": name, "password": PASSWORD}, token
    )
    assert started.status_code == 200, started.text
    return _post(
        client,
        "/settings/security-keys",
        {"credential": key.create(started.json()["options"])},
        token,
    )


def _logout(client: TestClient) -> None:
    client.post("/logout", headers={"X-CSRF-Token": _token(client), "Origin": ORIGIN})
    assert client.get("/").status_code == 303


def _key_login(client: TestClient, key: SoftKey, **kwargs: Any) -> Any:
    token = _token(client, "/login")
    started = _post(client, "/login/key/options", {}, token)
    assert started.status_code == 200, started.text
    return _post(
        client, "/login/key", {"credential": key.get(started.json()["options"], **kwargs)}, token
    )


def _set_mode(client: TestClient, mode: str) -> None:
    response = client.post(
        "/settings/login-mode",
        data={"mode": mode, "password": PASSWORD, "csrf_token": _token(client, "/settings")},
    )
    assert response.status_code == 303


def test_needs_a_host_name_origin(monkeypatch: pytest.MonkeyPatch, configured: Settings) -> None:
    monkeypatch.setenv("MIRRORHUB_PUBLIC_ORIGIN", "https://192.0.2.1:8443")
    from mirrorhub.settings import get_settings

    get_settings.cache_clear()
    assert "not an IP address" in (security_keys.available() or "")
    monkeypatch.setenv("MIRRORHUB_PUBLIC_ORIGIN", "")
    get_settings.cache_clear()
    assert "MIRRORHUB_PUBLIC_ORIGIN" in (security_keys.available() or "")


def test_register_keys(admin: TestClient) -> None:
    page = admin.get("/settings").text
    assert "Security keys (FIDO2)" in page and "Register a key" in page
    token = _token(admin, "/settings")
    wrong = _post(
        admin, "/settings/security-keys/options", {"name": "x", "password": "wrong"}, token
    )
    assert wrong.status_code == 403 and wrong.json()["error"] == "Wrong password"

    first = SoftKey()
    assert _register(admin, first, "YubiKey blue").json() == {"redirect": "/settings#security-keys"}
    page = admin.get("/settings").text
    assert "YubiKey blue" in page and "Only one key is registered" in page
    assert "Login mode" in page

    again = _register(admin, first, "same key")
    assert again.status_code == 400 and "already registered" in again.json()["error"]
    assert _register(admin, SoftKey(), "YubiKey spare").status_code == 200
    assert "Only one key is registered" not in admin.get("/settings").text

    with Session(get_engine()) as db:
        keys = db.exec(select(SecurityKey)).all()
        assert [key.name for key in keys] == ["YubiKey blue", "YubiKey spare"]
        assert keys[0].transports == ["usb"] and keys[0].sign_count == 1
        actions = [row.action for row in db.exec(select(AuditLog)).all()]
        assert actions.count("security_key.register") == 2
        assert "settings.password_confirm_failure" in actions


def test_registration_needs_the_pin(admin: TestClient) -> None:
    token = _token(admin, "/settings")
    started = _post(
        admin, "/settings/security-keys/options", {"name": "x", "password": PASSWORD}, token
    )
    options = started.json()["options"]
    assert options["authenticatorSelection"]["userVerification"] == "required"
    refused = _post(
        admin, "/settings/security-keys", {"credential": SoftKey().create(options, uv=False)}, token
    )
    assert refused.status_code == 400


def test_password_or_key(admin: TestClient) -> None:
    key = SoftKey()
    _register(admin, key)
    _logout(admin)
    assert "Sign in with security key" in admin.get("/login").text

    response = _key_login(admin, key)
    assert response.status_code == 200 and response.json() == {"redirect": "/"}
    assert admin.get("/").status_code == 200
    _logout(admin)

    assert _key_login(admin, key, uv=False).status_code == 401  # PIN required
    assert _key_login(admin, key, origin="https://evil.example").status_code == 401
    assert _key_login(admin, SoftKey()).status_code == 401  # not registered
    assert admin.get("/").status_code == 303

    # the password alone still works in this mode
    login = admin.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "csrf_token": _token(admin, "/login")},
    )
    assert login.status_code == 303 and admin.get("/").status_code == 200


def test_a_challenge_is_used_once(admin: TestClient) -> None:
    key = SoftKey()
    _register(admin, key)
    _logout(admin)
    token = _token(admin, "/login")
    options = _post(admin, "/login/key/options", {}, token).json()["options"]
    credential = key.get(options)
    cookie = admin.cookies.get("__Host-mh_wa")
    assert _post(admin, "/login/key", {"credential": credential}, token).status_code == 200
    _logout(admin)
    admin.cookies.set("__Host-mh_wa", cookie or "")
    replay = _post(admin, "/login/key", {"credential": credential}, _token(admin, "/login"))
    assert replay.status_code == 401 and "expired" in replay.json()["error"]


def test_password_and_key(admin: TestClient) -> None:
    key = SoftKey()
    _register(admin, key)
    _set_mode(admin, "both")
    _logout(admin)
    assert "Sign in with security key" not in admin.get("/login").text
    refused = _post(admin, "/login/key/options", {}, _token(admin, "/login"))
    assert refused.status_code == 400

    step = admin.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "csrf_token": _token(admin, "/login")},
    )
    assert step.status_code == 200 and "The password was correct" in step.text
    assert admin.get("/").status_code == 303  # no session yet
    match = OPTIONS_RE.search(step.text)
    assert match
    options = json.loads(html.unescape(match.group(1)))
    assert options["userVerification"] == "discouraged"
    token = TOKEN_RE.search(step.text)
    assert token
    done = _post(admin, "/login/key", {"credential": key.get(options, uv=False)}, token.group(1))
    assert done.status_code == 200, done.text
    assert admin.get("/").status_code == 200
    with Session(get_engine()) as db:
        success = [row for row in db.exec(select(AuditLog)).all() if row.action == "login.success"]
        assert success[-1].detail["method"] == "password + security key"


def test_failed_key_logins_lock_the_address(admin: TestClient, configured: Settings) -> None:
    key = SoftKey()
    _register(admin, key)
    _logout(admin)
    codes = [
        _key_login(admin, key, uv=False).status_code for _ in range(configured.login_max_attempts)
    ]
    assert codes[-1] == 429 and set(codes[:-1]) == {401}


def test_remove_key_and_reset(admin: TestClient) -> None:
    key = SoftKey()
    _register(admin, key)
    with Session(get_engine()) as db:
        key_id = db.exec(select(SecurityKey)).one().id
    wrong = admin.post(
        f"/settings/security-keys/{key_id}/delete",
        data={"password": "nope", "csrf_token": _token(admin, "/settings")},
    )
    assert wrong.status_code == 403
    removed = admin.post(
        f"/settings/security-keys/{key_id}/delete",
        data={"password": PASSWORD, "csrf_token": _token(admin, "/settings")},
    )
    assert removed.status_code == 303
    assert "Login mode" not in admin.get("/settings").text

    _register(admin, SoftKey())
    _set_mode(admin, "both")
    with Session(get_engine()) as db:
        assert security_keys.second_factor_required(db)
        assert security_keys.remove_all(db) == 1
        assert security_keys.login_mode(db) == "either"
        assert not security_keys.second_factor_required(db)
