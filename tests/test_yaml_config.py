"""Config-as-code: export shape, import planning, conflicts and signature opt-in."""

import pytest
import yaml
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Repository, Schedule

MINIMAL_YAML = """\
mirrorhub_config: 1
repositories:
  - slug: debian-test
    name: Debian test
    kind: apt
    tool: debmirror
    method: https
    upstream_host: deb.debian.org
    upstream_root: /debian
    suites: [bookworm]
    sections: [main]
    architectures: [amd64]
"""


def _post(admin: TestClient, token: str, data: dict[str, str]) -> Response:
    return admin.post("/config/import", data=data, headers={"X-CSRF-Token": token})


def _repos() -> list[Repository]:
    with Session(get_engine()) as db:
        return list(db.exec(select(Repository)).all())


def test_export_document_shape(admin: TestClient) -> None:
    response = admin.get("/config/export.yaml")
    assert response.status_code == 200
    document = yaml.safe_load(response.text)
    assert document["mirrorhub_config"] == 1
    assert isinstance(document["repositories"], list)
    assert "schedules" in document
    assert "Content-Disposition" in response.headers


def test_import_creates_repository(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, {"yaml_text": MINIMAL_YAML})
    assert response.status_code == 200
    assert "Create: 1" in response.text
    assert _repos() == []

    applied = _post(admin, csrf_token, {"yaml_text": MINIMAL_YAML, "apply": "1"})
    assert applied.status_code == 200
    assert [repo.slug for repo in _repos()] == ["debian-test"]
    assert _repos()[0].diff_mode == "use"
    assert _repos()[0].export_format == "tar.zst"


def test_conflict_is_refused_without_overwrite(admin: TestClient, csrf_token: str) -> None:
    _post(admin, csrf_token, {"yaml_text": MINIMAL_YAML, "apply": "1"})
    response = _post(
        admin,
        csrf_token,
        {"yaml_text": MINIMAL_YAML.replace("Debian test", "Changed"), "apply": "1"},
    )
    assert response.status_code == 200
    assert "Conflicts: 1" in response.text
    assert _repos()[0].name == "Debian test"


def test_overwrite_updates_existing(admin: TestClient, csrf_token: str) -> None:
    _post(admin, csrf_token, {"yaml_text": MINIMAL_YAML, "apply": "1"})
    response = _post(
        admin,
        csrf_token,
        {
            "yaml_text": MINIMAL_YAML.replace("Debian test", "Changed"),
            "apply": "1",
            "overwrite": "on",
        },
    )
    assert response.status_code == 200
    assert _repos()[0].name == "Changed"


def test_signature_disable_needs_explicit_opt_in(admin: TestClient, csrf_token: str) -> None:
    text = MINIMAL_YAML + "    verify_signatures: false\n"
    refused = _post(admin, csrf_token, {"yaml_text": text, "apply": "1"})
    assert refused.status_code == 400
    assert "signature checking is disabled" in refused.text
    assert _repos() == []

    allowed = _post(
        admin,
        csrf_token,
        {"yaml_text": text, "apply": "1", "allow_disable_signatures": "on"},
    )
    assert allowed.status_code == 200
    assert _repos()[0].verify_signatures is False


def test_import_creates_schedule(admin: TestClient, csrf_token: str) -> None:
    text = (
        MINIMAL_YAML
        + """\
schedules:
  - repo_slug: debian-test
    job_type: sync
    cron_expr: "0 3 * * *"
"""
    )
    response = _post(admin, csrf_token, {"yaml_text": text, "apply": "1"})
    assert response.status_code == 200
    with Session(get_engine()) as db:
        schedules = list(db.exec(select(Schedule)).all())
    assert len(schedules) == 1
    assert schedules[0].cron_expr == "0 3 * * *"


def test_invalid_yaml_root_is_rejected(admin: TestClient, csrf_token: str) -> None:
    response = _post(admin, csrf_token, {"yaml_text": "repositories: []"})
    assert response.status_code == 400
    assert "mirrorhub_config" in response.text


def test_unknown_repository_in_schedule_is_reported(admin: TestClient, csrf_token: str) -> None:
    text = (
        MINIMAL_YAML
        + """\
schedules:
  - repo_slug: does-not-exist
    job_type: sync
    cron_expr: "0 3 * * *"
"""
    )
    response = _post(admin, csrf_token, {"yaml_text": text, "apply": "1"})
    assert response.status_code == 200
    assert "unknown repository" in response.text


@pytest.mark.parametrize(
    ("line", "expected"),
    [("", None), ("    chain_export: null\n", None), ("    chain_export: true\n", True)],
)
def test_import_schedule_chain_export_tri_state(
    admin: TestClient, csrf_token: str, line: str, expected: bool | None
) -> None:
    text = (
        MINIMAL_YAML
        + """\
schedules:
  - repo_slug: debian-test
    job_type: sync
    cron_expr: "0 3 * * *"
"""
        + line
    )
    response = _post(admin, csrf_token, {"yaml_text": text, "apply": "1"})
    assert response.status_code == 200
    with Session(get_engine()) as db:
        schedule = db.exec(select(Schedule)).one()
    assert schedule.chain_export is expected


def test_import_rejects_non_boolean_chain_export(admin: TestClient, csrf_token: str) -> None:
    text = (
        MINIMAL_YAML
        + """\
schedules:
  - repo_slug: debian-test
    job_type: sync
    cron_expr: "0 3 * * *"
    chain_export: "yes"
"""
    )
    response = _post(admin, csrf_token, {"yaml_text": text, "apply": "1"})
    assert "chain_export must be true, false or null" in response.text


def test_import_ftpsync_without_architectures_mirrors_all(
    admin: TestClient, csrf_token: str
) -> None:
    document = """\
mirrorhub_config: 1
repositories:
  - slug: debian-ftpsync
    name: Debian FTPsync
    kind: apt
    tool: ftpsync
    method: rsync
    upstream_host: deb.debian.org
    upstream_root: /debian
"""
    applied = _post(admin, csrf_token, {"yaml_text": document, "apply": "1"})
    assert applied.status_code == 200

    repos = _repos()
    assert [repo.slug for repo in repos] == ["debian-ftpsync"]
    assert repos[0].architectures == []
    assert repos[0].arch_exclude == []


def test_wget_filters_round_trip(admin: TestClient, csrf_token: str) -> None:
    document = """\
mirrorhub_config: 1
repositories:
  - slug: docker-tree
    name: Docker tree
    kind: generic
    tool: wget
    method: https
    upstream_host: download.docker.com
    upstream_root: /linux/ubuntu
    include_regex: ''
    exclude_regex: '/(test|nightly)/'
    wget_keep_arches: [amd64, i386]
"""
    applied = _post(admin, csrf_token, {"yaml_text": document, "apply": "1"})
    assert applied.status_code == 200
    repo = _repos()[0]
    assert repo.exclude_regex == "/(test|nightly)/"
    assert repo.wget_keep_arches == ["amd64", "i386"]

    exported = yaml.safe_load(admin.get("/config/export.yaml").text)["repositories"][0]
    assert exported["exclude_regex"] == "/(test|nightly)/"
    assert exported["wget_keep_arches"] == ["amd64", "i386"]


def test_wget_unknown_keep_arch_is_an_import_error(admin: TestClient, csrf_token: str) -> None:
    document = """\
mirrorhub_config: 1
repositories:
  - slug: files
    name: Files
    kind: generic
    tool: wget
    method: https
    upstream_host: example.org
    upstream_root: /pub
    wget_keep_arches: [sparc]
"""
    response = _post(admin, csrf_token, {"yaml_text": document, "apply": "1"})
    assert "architectures to keep: unknown" in response.text
    assert _repos() == []


def test_wget_custom_args_round_trip(admin: TestClient, csrf_token: str) -> None:
    document = """\
mirrorhub_config: 1
repositories:
  - slug: tree
    name: Tree
    kind: generic
    tool: wget
    method: https
    upstream_host: example.org
    upstream_root: /pub
    wget_custom_args: [--recursive, --level=inf, --no-parent, --wait=2]
"""
    applied = _post(admin, csrf_token, {"yaml_text": document, "apply": "1"})
    assert applied.status_code == 200
    assert _repos()[0].wget_custom_args == ["--recursive", "--level=inf", "--no-parent", "--wait=2"]
    exported = yaml.safe_load(admin.get("/config/export.yaml").text)["repositories"][0]
    assert exported["wget_custom_args"][-1] == "--wait=2"


def test_wget_custom_args_are_checked_on_import(admin: TestClient, csrf_token: str) -> None:
    document = """\
mirrorhub_config: 1
repositories:
  - slug: tree
    name: Tree
    kind: generic
    tool: wget
    method: https
    upstream_host: example.org
    upstream_root: /pub
    wget_custom_args: [--recursive, --use-askpass=/bin/sh]
"""
    response = _post(admin, csrf_token, {"yaml_text": document, "apply": "1"})
    assert "--use-askpass is not allowed" in response.text
    assert _repos() == []
