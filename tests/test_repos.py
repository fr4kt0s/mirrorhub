"""Repository CRUD through the web UI. No subprocess calls, no GPG involvement."""

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
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
    "arch_exclude": "",
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


def _post(admin: TestClient, token: str, path: str, data: dict[str, str]) -> Response:
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


def _wget_form(**overrides: str) -> dict[str, str]:
    form = _form(
        slug="files",
        name="Files",
        kind="generic",
        tool="wget",
        method="https",
        upstream_host="example.org",
        upstream_root="/pub",
        suites="",
        sections="",
        architectures="none",
        extra_files="",
    )
    form.update(overrides)
    return form


def test_wget_tree_mode_needs_no_file_entries(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _wget_form(wget_mode="tree"))
    assert response.status_code == 303
    saved = _repos()
    assert [repo.slug for repo in saved] == ["files"]
    assert saved[0].extra_files == []


def test_wget_without_mode_field_defaults_to_tree(admin: TestClient, csrf_token: str) -> None:
    """Older forms and API clients without the switch: an empty list means the whole tree."""
    response = _post(admin, csrf_token, "/repos", _wget_form())
    assert response.status_code == 303
    assert _repos()[0].extra_files == []


def test_wget_tree_mode_ignores_file_list(admin: TestClient, csrf_token: str) -> None:
    form = _wget_form(wget_mode="tree", extra_files="https://example.org/pub/a.iso")
    assert _post(admin, csrf_token, "/repos", form).status_code == 303
    assert _repos()[0].extra_files == []


def test_wget_file_mode_requires_entries(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, "/repos", _wget_form(wget_mode="files"))
    assert response.status_code == 400
    assert "file list: enter at least one file URL, one per line" in response.text
    assert _repos() == []


def test_wget_file_mode_one_url_per_line(admin: TestClient, csrf_token: str) -> None:
    form = _wget_form(
        wget_mode="files",
        extra_files=(
            "https://example.org/pub/iso/a.iso\n"
            "\n"
            "https://example.org/keys/release.asc -> keys/archive.asc\n"
        ),
    )
    assert _post(admin, csrf_token, "/repos", form).status_code == 303
    assert _repos()[0].extra_files == [
        {"url": "https://example.org/pub/iso/a.iso", "path": "pub/iso/a.iso"},
        {"url": "https://example.org/keys/release.asc", "path": "keys/archive.asc"},
    ]
    repo_id = _repos()[0].id
    page = admin.get(f"/repos/{repo_id}/edit").text
    assert 'value="files" checked' in page
    assert "https://example.org/pub/iso/a.iso -&gt; pub/iso/a.iso" in page


def test_wget_file_mode_rejects_url_without_file_name(admin: TestClient, csrf_token: str) -> None:
    form = _wget_form(wget_mode="files", extra_files="https://example.org/pub/")
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "cannot derive a file name" in response.text


def test_wget_file_mode_rejects_duplicate_paths(admin: TestClient, csrf_token: str) -> None:
    form = _wget_form(
        wget_mode="files",
        extra_files="https://a.example.org/x/a.iso\nhttps://b.example.org/x/a.iso",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "several entries write to the same path: x/a.iso" in response.text


def test_wget_file_mode_accepts_500_files(admin: TestClient, csrf_token: str) -> None:
    lines = "\n".join(f"https://host{i % 7}.example.org/pub/f{i}.bin" for i in range(500))
    form = _wget_form(wget_mode="files", extra_files=lines)
    assert _post(admin, csrf_token, "/repos", form).status_code == 303
    assert len(_repos()[0].extra_files) == 500


def test_wget_file_mode_rejects_501_files(admin: TestClient, csrf_token: str) -> None:
    lines = "\n".join(f"https://example.org/pub/f{i}.bin" for i in range(501))
    form = _wget_form(wget_mode="files", extra_files=lines)
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "extra files: at most 500 entries" in response.text
    assert "At most 500 files." in response.text


def test_wget_edit_page_shows_tree_mode(admin: TestClient, csrf_token: str) -> None:
    assert _post(admin, csrf_token, "/repos", _wget_form(wget_mode="tree")).status_code == 303
    page = admin.get(f"/repos/{_repos()[0].id}/edit").text
    assert 'value="tree" checked' in page


def test_debmirror_requires_suites_and_sections(admin: TestClient, csrf_token: str) -> None:
    form = _form(suites="", sections="")
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "at least one suite" in response.text
    assert "sections/components" in response.text


def test_ftpsync_rejects_include_and_exclude(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="debian-ftpsync",
        name="Debian FTPsync",
        kind="apt",
        tool="ftpsync",
        method="rsync",
        upstream_host="deb.debian.org",
        upstream_root="/debian",
        suites="",
        sections="",
        architectures="amd64",
        arch_exclude="armel",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "use either ARCH_INCLUDE or ARCH_EXCLUDE" in response.text


@pytest.mark.parametrize(
    ("include", "exclude"),
    [("none", ""), ("", "none")],
)
def test_ftpsync_rejects_none_architecture(
    admin: TestClient, csrf_token: str, include: str, exclude: str
) -> None:
    form = _form(
        slug="debian-ftpsync",
        name="Debian FTPsync",
        kind="apt",
        tool="ftpsync",
        method="rsync",
        upstream_host="deb.debian.org",
        upstream_root="/debian",
        suites="",
        sections="",
        architectures=include,
        arch_exclude=exclude,
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "is not an ftpsync architecture" in response.text
    assert _repos() == []


def test_ftpsync_validates_arch_include_entries(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="debian-ftpsync",
        name="Debian FTPsync",
        kind="apt",
        tool="ftpsync",
        method="rsync",
        upstream_host="deb.debian.org",
        upstream_root="/debian",
        suites="",
        sections="",
        architectures="amd64 $(id)",
        arch_exclude="",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 400
    assert "architectures to include: invalid entry" in response.text
    assert _repos() == []


def test_ftpsync_accepts_arch_exclude(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="debian-ftpsync",
        name="Debian FTPsync",
        kind="apt",
        tool="ftpsync",
        method="rsync",
        upstream_host="deb.debian.org",
        upstream_root="/debian",
        suites="",
        sections="",
        architectures="",
        arch_exclude="armel armhf",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 303

    repos = _repos()
    assert repos[-1].arch_exclude == ["armel", "armhf"]


def test_ftpsync_allows_empty_architecture_selection(admin: TestClient, csrf_token: str) -> None:
    form = _form(
        slug="debian-ftpsync",
        name="Debian FTPsync",
        kind="apt",
        tool="ftpsync",
        method="rsync",
        upstream_host="deb.debian.org",
        upstream_root="/debian",
        suites="",
        sections="",
        architectures="",
        arch_exclude="",
    )
    response = _post(admin, csrf_token, "/repos", form)
    assert response.status_code == 303

    repos = _repos()
    assert repos[-1].architectures == []
    assert repos[-1].arch_exclude == []


@pytest.mark.parametrize("path", ["/repos", "/presets", "/keys", "/config"])
def test_pages_require_login(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
