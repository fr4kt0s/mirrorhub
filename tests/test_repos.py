"""Repository CRUD through the web UI. No subprocess calls, no GPG involvement."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog, Preset, Repository

BASE_FORM: dict[str, str] = {
    "slug": "debian-bookworm",
    "name": "Debian bookworm",
    "kind": "apt",
    "tool": "debmirror",
    "method": "https",
    "upstream_host": "deb.debian.org",
    "upstream_root": "/debian",
    "suites": "bookworm, bookworm-updates",
    "sections": "main, contrib",
    "architectures": "amd64",
    "include_source": "",
    "rsync_extra": "",
    "bwlimit_kbps": "0",
    "diff_mode": "use",
    "cleanup_mode": "post",
    "verify_signatures": "on",
    "include_patterns": "",
    "exclude_patterns": "",
    "extra_args": "",
    "extra_files": "",
    "retry_max_attempts": "3",
    "retry_backoff": "exponential",
    "retry_base_seconds": "60",
    "retry_max_seconds": "3600",
    "retry_exit_codes": "1,2,255",
    "export_mode": "incr",
    "export_format": "tar.zst",
    "export_split_mb": "0",
    "chain_export": "",
    "enabled": "on",
    "sig_off_confirm": "",
    "save_as_preset": "",
    "preset_name": "",
}


def _form(**overrides: str) -> dict[str, str]:
    form = dict(BASE_FORM)
    form.update(overrides)
    return form


def _repos() -> list[Repository]:
    with Session(get_engine()) as db:
        return list(db.exec(select(Repository)).all())


def _post(admin: TestClient, token: str, path: str, data: dict[str, str]) -> object:
    return admin.post(path, data=data, headers={"X-CSRF-Token": token})


def test_post_without_session_is_rejected(client: TestClient) -> None:
    """No session and no CSRF token: the state-changing request stops at 403."""
    response = client.post("/repos", data=_form())
    assert response.status_code == 403
    assert _repos() == []


def test_create_repository(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _form())
    assert response.status_code == 303
    saved = _repos()
    assert [repo.slug for repo in saved] == ["debian-bookworm"]
    assert saved[0].suites == ["bookworm", "bookworm-updates"]
    assert saved[0].sections == ["main", "contrib"]
    assert "debian-bookworm" in admin.get("/repos").text


def test_duplicate_slug_is_rejected(admin: TestClient, csrf_token: str) -> None:
    assert _post(admin, csrf_token, "/repos", _form()).status_code == 303
    response = _post(admin, csrf_token, "/repos", _form(name="Another"))
    assert response.status_code == 400
    assert "already used" in response.text
    assert len(_repos()) == 1


def test_invalid_host_is_rejected(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _form(upstream_host="https://deb.debian.org"))
    assert response.status_code == 400
    assert "upstream host" in response.text
    assert _repos() == []


def test_trailing_slash_in_root_is_rejected(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _form(upstream_root="/debian/"))
    assert response.status_code == 400
    assert "trailing slash" in response.text


def test_none_architecture_cannot_be_combined(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _form(architectures="none, amd64"))
    assert response.status_code == 400
    assert "cannot be combined" in response.text


def test_disallowed_extra_argument_is_rejected(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _form(extra_args="--ignore-release-gpg"))
    assert response.status_code == 400
    assert "not allowed for debmirror" in response.text


def test_disabling_signature_checking_requires_the_phrase(
    admin: TestClient, csrf_token: str
) -> None:
    response = _post(admin, csrf_token, "/repos", _form(verify_signatures=""))
    assert response.status_code == 400
    assert "I understand" in response.text
    assert _repos() == []


def test_disabling_signature_checking_is_audited(admin: TestClient, csrf_token: str) -> None:
    response = _post(
        admin,
        csrf_token,
        "/repos",
        _form(verify_signatures="", sig_off_confirm="I understand"),
    )
    assert response.status_code == 303
    with Session(get_engine()) as db:
        actions = [row.action for row in db.exec(select(AuditLog)).all()]
    assert "repo.create" in actions
    assert "repo.signature_checking_disabled" in actions


def test_update_repository(admin: TestClient, csrf_token: str) -> None:
    assert _post(admin, csrf_token, "/repos", _form()).status_code == 303
    repo = _repos()[0]
    response = _post(admin, csrf_token, f"/repos/{repo.id}", _form(name="Renamed"))
    assert response.status_code == 303
    assert _repos()[0].name == "Renamed"


def test_delete_repository(admin: TestClient, csrf_token: str) -> None:
    assert _post(admin, csrf_token, "/repos", _form()).status_code == 303
    repo = _repos()[0]
    response = _post(admin, csrf_token, f"/repos/{repo.id}/delete", {})
    assert response.status_code == 303
    assert _repos() == []


def test_save_as_preset(admin: TestClient, csrf_token: str) -> None:
    response = _post(
        admin,
        csrf_token,
        "/repos",
        _form(save_as_preset="on", preset_name="My Debian template"),
    )
    assert response.status_code == 303
    with Session(get_engine()) as db:
        preset = db.exec(select(Preset).where(Preset.name == "My Debian template")).first()
    assert preset is not None
    assert preset.config["upstream_root"] == "/debian"


def test_preset_page_and_clone_prefill(admin: TestClient) -> None:
    page = admin.get("/presets")
    assert page.status_code == 200
    assert "Debian 12 (bookworm)" in page.text
    with Session(get_engine()) as db:
        preset = db.exec(select(Preset).where(Preset.name == "Ubuntu 24.04 LTS (noble)")).first()
    assert preset is not None
    form = admin.get(f"/repos/new?preset={preset.id}")
    assert form.status_code == 200
    assert "archive.ubuntu.com" in form.text
    assert "noble" in form.text


def test_rsync_requires_rsync_method(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="archlinux",
        name="Arch Linux",
        kind="arch",
        tool="rsync",
        method="https",
        upstream_root="/packages",
        suites="core",
        sections="",
        architectures="x86_64",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "rsync tool requires method rsync" in response.text


def test_rsync_requires_module(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="archlinux",
        name="Arch Linux",
        kind="arch",
        tool="rsync",
        method="rsync",
        upstream_root="/",
        suites="core",
        sections="",
        architectures="x86_64",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "must start with the rsync module" in response.text


def test_wget_requires_http_or_https(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="files",
        name="Files",
        kind="generic",
        tool="wget",
        method="rsync",
        upstream_root="/",
        suites="",
        sections="",
        architectures="none",
        extra_files="https://example.org/file.asc -> keys/archive.asc",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "wget supports http and https only" in response.text


def test_wget_requires_entries(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="files",
        name="Files",
        kind="generic",
        tool="wget",
        method="https",
        upstream_root="/",
        suites="",
        sections="",
        architectures="none",
        extra_files="",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "wget mirrors need at least one URL and relative path entry" in response.text


def test_debmirror_requires_suites_and_sections(admin: TestClient, csrf_token: str) -> None:
    form = _form(suites="", sections="")
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "at least one suite" in response.text
    assert "sections/components" in response.text


@pytest.mark.parametrize("path", ["/repos", "/presets", "/keys", "/config"])
def test_pages_require_login(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
