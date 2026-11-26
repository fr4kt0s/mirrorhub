"""#43 on the sender: checking a repair request and widening the export (no database)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from mirrorhub.export import repair
from mirrorhub.export.manifest import ExportError
from mirrorhub.importer import repair as receiver

BASE = "20261101T000000Z-full"
FILE = {"type": "file", "size": 1, "mtime_ns": 1, "sha256": "0" * 64}


def _written(tmp_path: Path, found: list[str]) -> tuple[bytes, bytes]:
    """A request exactly as the importer writes it."""
    path = receiver.write_request(tmp_path, "ubuntu", BASE, found, hashed=False)
    assert path is not None
    return path.read_bytes(), path.with_name(path.name + ".sha256").read_bytes()


def _signed(document: dict[str, Any]) -> tuple[bytes, bytes]:
    data = json.dumps(document).encode()
    return data, f"{hashlib.sha256(data).hexdigest()}  repair-ubuntu.json\n".encode()


def _document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": "mirrorhub-repair-request",
        "version": 1,
        "slug": "ubuntu",
        "base_export_id": BASE,
        "created_at": "2026-11-26T09:00:00+00:00",
        "check": "quick",
        "importer_commit": None,
        "resend": ["pool/a.deb"],
        "delete": ["pool/x.deb"],
        "skipped": 0,
    }
    document.update(changes)
    return document


def test_the_importers_request_is_accepted(tmp_path: Path) -> None:
    data, checksum = _written(
        tmp_path, ["missing: pool/a.deb", "sha256: pool/b.deb", "extra: pool/x.deb"]
    )
    request = repair.parse(data, checksum, "ubuntu")
    assert request.base_export_id == BASE and request.check == "quick"
    assert request.resend == ("pool/a.deb", "pool/b.deb") and request.delete == ("pool/x.deb",)
    assert request.sha256 == hashlib.sha256(data).hexdigest()
    # sha256sum -c writes '*' for binary mode; that is accepted too
    starred = checksum.replace(b"  ", b"  *")
    assert repair.parse(data, starred, "ubuntu") == request


@pytest.mark.parametrize(
    ("data", "checksum", "message"),
    [
        (b"", b"x", "empty"),
        (b"{}", b"0" * 64 + b"  repair-ubuntu.json", "does not match its .sha256"),
        (b"{}", b"nonsense", "one line"),
    ],
)
def test_broken_uploads_are_refused(data: bytes, checksum: bytes, message: str) -> None:
    with pytest.raises(ExportError, match=message):
        repair.parse(data, checksum, "ubuntu")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format": "other"}, "not a MirrorHub repair request"),
        ({"version": 2}, "unsupported"),
        ({"slug": "debian"}, "for repository 'debian'"),
        ({"base_export_id": "../x"}, "invalid base_export_id"),
        ({"resend": ["../etc/passwd"]}, "unsafe relative path"),
        ({"resend": ["/abs"]}, "unsafe relative path"),
        ({"delete": "pool/x.deb"}, "must be a list"),
        ({"resend": ["a", "a"]}, "twice"),
        ({"delete": ["pool/a.deb"]}, "to resend and to delete"),
        ({"resend": [], "delete": []}, "nothing to repair"),
        ({"skipped": -1}, "count"),
    ],
)
def test_bad_requests_are_refused(changes: dict[str, Any], message: str) -> None:
    data, checksum = _signed(_document(**changes))
    with pytest.raises(ExportError, match=message):
        repair.parse(data, checksum, "ubuntu")


def test_a_request_of_another_repository_is_named(tmp_path: Path) -> None:
    data, checksum = _written(tmp_path, ["missing: pool/a.deb"])
    with pytest.raises(ExportError, match=r"repair-debian\.json"):
        repair.parse(data, checksum, "debian")


def test_paths_must_fit_the_base() -> None:
    request = repair.parse(*_signed(_document()), "ubuntu")
    repair.check_against_base(request, {"pool/a.deb": FILE})
    with pytest.raises(ExportError, match="not in 20261101T000000Z-full"):
        repair.check_against_base(request, {})
    with pytest.raises(ExportError, match="extra path"):
        repair.check_against_base(request, {"pool/a.deb": FILE, "pool/x.deb": FILE})


def test_apply_widens_payload_and_deletions() -> None:
    request = repair.parse(
        *_signed(_document(resend=["pool/a.deb", "pool/gone.deb"], delete=["pool/x.deb", "new"])),
        "ubuntu",
    )
    current = {"pool/a.deb": FILE, "pool/c.deb": FILE, "new": FILE}
    applied = repair.apply(
        request, current, ["new", "pool/c.deb"], ["pool/gone.deb"], keep_extra=False
    )
    assert applied.changed == ["new", "pool/a.deb", "pool/c.deb"]
    assert applied.deleted == ["pool/gone.deb", "pool/x.deb"]  # "new" exists: payload
    assert applied.resent == 1 and applied.removed == 1
    assert applied.field["request_sha256"] == request.sha256 and not applied.field["keep_extra"]

    kept = repair.apply(request, current, [], [], keep_extra=True)
    assert kept.deleted == [] and kept.field == {**kept.field, "delete": 0, "keep_extra": True}


def test_kept_requests(tmp_path: Path) -> None:
    data, checksum = _written(tmp_path / "receiver", ["missing: pool/a.deb"])
    request = repair.parse(data, checksum, "ubuntu")
    path = repair.store(tmp_path, request, data)
    assert repair.load(tmp_path, "ubuntu", request.sha256) == request
    with pytest.raises(ExportError, match="invalid"):
        repair.load(tmp_path, "ubuntu", "../x")
    repair.discard(tmp_path, "ubuntu", request.sha256)
    with pytest.raises(ExportError, match="upload it again"):
        repair.load(tmp_path, "ubuntu", request.sha256)

    repair.store(tmp_path, request, data)
    os.utime(path, (1, 1))  # a week old
    other = repair.parse(*_signed(_document()), "ubuntu")
    repair.store(tmp_path, other, _signed(_document())[0])
    assert not path.exists()
