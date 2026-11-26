"""#43 end to end: receiver request -> upload on the sender -> repair export -> import."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.export import job as engine
from mirrorhub.export.manifest import ExportError
from mirrorhub.importer.cli import main as importer
from mirrorhub.importer.state import StateStore
from mirrorhub.models import Job, Repository
from mirrorhub.services import export_jobs
from mirrorhub.settings import Settings
from mirrorhub.verify import verify
from test_export_base import _export, _pulse, _repository, _write


def _receiver(settings: Settings, tmp_path: Path) -> tuple[list[str], Path]:
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    config = receiver / "importer.toml"
    config.write_text(
        f'export_dir = "{settings.export_dir}"\nmirror_root = "mirror"\n'
        'state_dir = "state"\nreports_dir = "reports"\n',
        encoding="utf-8",
    )
    return ["--config", str(config)], receiver


def _damaged_receiver(settings: Settings, tmp_path: Path) -> tuple[Repository, list[str], Path]:
    repo = _repository(settings)
    _write(settings, "second.txt", b"second")
    _export(repo, settings)
    base, receiver = _receiver(settings, tmp_path)
    assert importer([*base, "import", "--slug", "files"]) == 0
    tree = receiver / "mirror" / "files"
    (tree / "first.txt").unlink()
    (tree / "second.txt").write_bytes(b"SECOND!")
    (tree / "extra.txt").write_bytes(b"extra")
    assert importer([*base, "check", "--slug", "files"]) == 11
    return repo, base, receiver


def _request_files(receiver: Path) -> dict[str, tuple[str, bytes, str]]:
    folder = receiver / "reports" / "files"
    return {
        "request": ("repair-files.json", (folder / "repair-files.json").read_bytes(), "text/plain"),
        "checksum": (
            "repair-files.json.sha256",
            (folder / "repair-files.json.sha256").read_bytes(),
            "text/plain",
        ),
    }


def _run_queued(repo: Repository, settings: Settings) -> str:
    with Session(get_engine()) as db:
        row = db.exec(select(Job).where(Job.type == "export", Job.status == "queued")).one()
        row.status = "succeeded"  # executed directly below
        db.add(row)
        db.commit()
        db.refresh(row)
        job = Job(**row.model_dump())
    time.sleep(1.05)  # export ids have one-second resolution
    return engine.execute(job, repo, settings, _pulse)


def test_repair_round_trip(
    admin: TestClient, csrf_token: str, configured: Settings, tmp_path: Path
) -> None:
    repo, base, receiver = _damaged_receiver(configured, tmp_path)
    _write(configured, "third.txt", b"third")  # the sender moved on meanwhile
    headers = {"X-CSRF-Token": csrf_token}

    page = admin.post(f"/exports/{repo.id}/repair", files=_request_files(receiver), headers=headers)
    assert page.status_code == 200, page.text
    assert "Files to send again</th><td>2" in page.text
    assert "Extra files on the receiver</th><td>1 (deleted)" in page.text
    request = json.loads((receiver / "reports" / "files" / "repair-files.json").read_bytes())
    assert request["resend"] == ["first.txt", "second.txt"]

    request_id = page.text.split('name="request" value="')[1][:64]
    started = admin.post(
        f"/exports/{repo.id}/repair/start", data={"request": request_id}, headers=headers
    )
    assert started.status_code == 303
    with Session(get_engine()) as db:
        job = db.exec(select(Job).where(Job.status == "queued")).one()
        assert job.params["repair"] == request_id and job.params["keep_extra"] is False

    export_id = _run_queued(repo, configured)
    metadata = verify(configured.export_dir / "files" / export_id)  # the verifier accepts it
    assert metadata["mode"] == "incr"
    assert metadata["payload_paths"] == ["first.txt", "second.txt", "third.txt"]
    assert metadata["repair"]["resend"] == 2 and metadata["repair"]["delete"] == 1
    deleted = (configured.export_dir / "files" / export_id / "deleted.txt").read_text()
    assert deleted == "extra.txt\n"
    assert not list((configured.manifest_dir / "repair-requests" / "files").glob("*.json"))

    assert importer([*base, "import", "--slug", "files"]) == 0
    tree = receiver / "mirror" / "files"
    assert sorted(path.name for path in tree.iterdir()) == ["first.txt", "second.txt", "third.txt"]
    assert (tree / "second.txt").read_bytes() == b"second"
    assert StateStore(receiver / "state", "files").load().history[-1]["repair"] is True
    assert importer([*base, "check", "--slug", "files"]) == 0
    assert not (receiver / "reports" / "files" / "repair-files.json").exists()


def test_kept_extra_files_stay(
    admin: TestClient, csrf_token: str, configured: Settings, tmp_path: Path
) -> None:
    repo, base, receiver = _damaged_receiver(configured, tmp_path)
    headers = {"X-CSRF-Token": csrf_token}
    page = admin.post(
        f"/exports/{repo.id}/repair",
        files=_request_files(receiver),
        data={"keep_extra": "on"},
        headers=headers,
    )
    assert "1 (<strong>kept</strong>)" in page.text
    request_id = page.text.split('name="request" value="')[1][:64]
    admin.post(
        f"/exports/{repo.id}/repair/start",
        data={"request": request_id, "keep_extra": "on"},
        headers=headers,
    )
    export_id = _run_queued(repo, configured)
    assert (configured.export_dir / "files" / export_id / "deleted.txt").read_text() == ""

    assert importer([*base, "import", "--slug", "files"]) == 11  # extra.txt still differs
    assert (receiver / "mirror" / "files" / "extra.txt").read_bytes() == b"extra"
    left = json.loads((receiver / "reports" / "files" / "repair-files.json").read_bytes())
    assert left["resend"] == [] and left["delete"] == ["extra.txt"]


def test_uploads_that_cannot_work_are_refused(
    admin: TestClient, csrf_token: str, configured: Settings, tmp_path: Path
) -> None:
    repo, _base, receiver = _damaged_receiver(configured, tmp_path)
    headers = {"X-CSRF-Token": csrf_token}
    files = _request_files(receiver)
    damaged = {**files, "request": (files["request"][0], files["request"][1] + b" ", "text/plain")}
    refused = admin.post(f"/exports/{repo.id}/repair", files=damaged, headers=headers)
    assert refused.status_code == 400 and "does not match its .sha256" in refused.text

    only_request = {"request": files["request"]}
    refused = admin.post(f"/exports/{repo.id}/repair", files=only_request, headers=headers)
    assert refused.status_code == 400 and "sha256 file" in refused.text

    refused = admin.post(
        f"/exports/{repo.id}/repair/start", data={"request": "0" * 64}, headers=headers
    )
    assert refused.status_code == 400 and "upload it again" in refused.text
    with pytest.raises(ExportError, match="invalid repair request id"):
        export_jobs.enqueue(repo.id or 0, mode="incr", actor="test", repair_request="../x")


def test_exports_page_offers_the_upload(admin: TestClient, configured: Settings) -> None:
    repo = _repository(configured)
    _export(repo, configured)
    page = admin.get("/exports")
    assert "Repair export" in page.text and f"/exports/{repo.id}/repair" in page.text
