"""Checksum manifests must cover payload files, not themselves."""

import hashlib
from pathlib import Path

import pytest

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


def test_known_part_digests_are_not_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirrorhub.export import manifest

    (tmp_path / "data.tar").write_bytes(b"archive")
    (tmp_path / "files.json").write_text("{}\n", encoding="utf-8")
    read: list[str] = []
    real = manifest.file_hash

    def counting(path: Path, pulse: object = None) -> str:
        read.append(path.name)
        return real(path)

    monkeypatch.setattr(manifest, "file_hash", counting)
    _write_checksums(tmp_path, _pulse, {"data.tar": "a" * 64})
    assert read == ["files.json"]
    assert f"{'a' * 64}  data.tar" in (tmp_path / "MANIFEST.sha256").read_text()
