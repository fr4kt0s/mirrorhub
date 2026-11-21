"""Configured public origins must match exactly."""

from collections.abc import Iterator

import pytest
from starlette.requests import Request

from mirrorhub.security.csrf import origin_ok
from mirrorhub.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    # origin_ok reads MIRRORHUB_PUBLIC_ORIGIN through the cached settings.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "root_path": "",
            "server": ("web", 8000),
            "client": ("127.0.0.1", 12345),
            "headers": [
                (b"host", b"web:8000"),
                (b"origin", origin.encode("ascii")),
            ],
        }
    )


def test_configured_origin_accepts_public_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MIRRORHUB_PUBLIC_ORIGIN",
        "https://localhost:8443",
    )
    get_settings.cache_clear()
    assert origin_ok(_request("https://localhost:8443"))


@pytest.mark.parametrize(
    "origin",
    [
        "https://untrusted.example",
        "https://localhost:9443",
        "http://localhost:8443",
        "https://127.0.0.1:8443",
        "https://localhost.attacker.example:8443",
        "null",
    ],
)
def test_configured_origin_rejects_other_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    monkeypatch.setenv(
        "MIRRORHUB_PUBLIC_ORIGIN",
        "https://localhost:8443",
    )
    get_settings.cache_clear()
    assert not origin_ok(_request(origin))
