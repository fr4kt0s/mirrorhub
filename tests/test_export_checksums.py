"""Checksum manifests must cover payload files, not themselves."""

import hashlib
from pathlib import Path

from mirrorhub.export.job import _write_checksums


def _pulse(_pct: float | None, _text: str) -> None:
    pass


def test_checksum_manifest_excludes_itself(tmp_path: Path) -> None:
    payload = b"example archive"
    (tmp_path / "data.tar").write_bytes(payload)
    (tmp_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "files.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("", encoding="utf-8")

    _write_checksums(tmp_path, _pulse)

    lines = (tmp_path / "MANIFEST.sha256").read_text(encoding="ascii").splitlines()
    entries = dict(line.split("  ", 1)[::-1] for line in lines)

    assert set(entries) == {
        "data.tar",
        "manifest.json",
        "files.json",
        "deleted.txt",
    }
    assert entries["data.tar"] == hashlib.sha256(payload).hexdigest()
    assert "MANIFEST.sha256" not in entries
    assert "MANIFEST.sha256.sig" not in entries
    assert ".done" not in entries
