"""Verify a MirrorHub export with the Python standard library only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = {"manifest.json", "files.json", "deleted.txt"}


class VerificationError(ValueError):
    """An export failed verification."""


def _relative(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and all(ord(character) >= 32 for character in value)
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify(directory: Path) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise VerificationError("export directory is missing or is a symlink")

    entries = list(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise VerificationError("export contains a symlink or non-file entry")

    names = {path.name for path in entries}
    if not REQUIRED | {"MANIFEST.sha256", ".done"} <= names:
        raise VerificationError("required export files are missing")

    checksums: dict[str, str] = {}
    for line in (directory / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or not DIGEST_RE.fullmatch(digest)
            or not _relative(name)
            or "/" in name
            or name in checksums
            or name in {".done", "MANIFEST.sha256", "MANIFEST.sha256.sig"}
        ):
            raise VerificationError("invalid checksum manifest")
        checksums[name] = digest

    expected = names - {".done", "MANIFEST.sha256", "MANIFEST.sha256.sig"}
    if set(checksums) != expected:
        raise VerificationError("checksum coverage does not match package contents")

    for name, digest in checksums.items():
        if _digest(directory / name) != digest:
            raise VerificationError(f"checksum mismatch: {name}")

    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    inventory = json.loads((directory / "files.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
        raise VerificationError("unsupported export format")
    if not isinstance(inventory, dict):
        raise VerificationError("invalid file inventory")
    if metadata.get("mode") not in ("full", "incr"):
        raise VerificationError("invalid export mode")
    if metadata["mode"] == "incr" and not metadata.get("base_export_id"):
        raise VerificationError("incremental export has no base")
    if metadata["mode"] == "full" and metadata.get("base_export_id") is not None:
        raise VerificationError("full export unexpectedly declares a base")

    for relative, entry in inventory.items():
        if (
            not isinstance(relative, str)
            or not _relative(relative)
            or not isinstance(entry, dict)
            or entry.get("type") not in ("file", "symlink")
            or not isinstance(entry.get("size"), int)
            or entry["size"] < 0
            or not isinstance(entry.get("mtime_ns"), int)
            or not isinstance(entry.get("sha256"), str)
            or not DIGEST_RE.fullmatch(entry["sha256"])
        ):
            raise VerificationError("invalid inventory entry")

    parts = metadata.get("archive_parts")
    if (
        not isinstance(parts, list)
        or not parts
        or any(not isinstance(name, str) or name not in checksums for name in parts)
        or len(parts) != len(set(parts))
    ):
        raise VerificationError("invalid archive part list")

    if set(parts) != set(checksums) - REQUIRED:
        raise VerificationError("archive part coverage mismatch")

    payload = metadata.get("payload_paths")
    if (
        not isinstance(payload, list)
        or any(not isinstance(path, str) or path not in inventory for path in payload)
        or len(payload) != len(set(payload))
    ):
        raise VerificationError("invalid payload path list")

    deleted = (directory / "deleted.txt").read_text(encoding="utf-8").splitlines()
    if any(not _relative(path) or path in inventory for path in deleted):
        raise VerificationError("invalid deletion list")
    if len(deleted) != len(set(deleted)):
        raise VerificationError("duplicate deletion entries")

    counts = metadata.get("counts", {})
    if counts != {
        "inventory": len(inventory),
        "payload": len(payload),
        "deleted": len(deleted),
    }:
        raise VerificationError("metadata counts do not match package contents")

    if metadata["mode"] == "full" and (deleted or set(payload) != set(inventory)):
        raise VerificationError("full export is incomplete")

    marker = (directory / ".done").read_text(encoding="ascii").strip()
    if marker != metadata.get("export_id"):
        raise VerificationError("completion marker does not match export id")

    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_directory", type=Path)
    arguments = parser.parse_args()
    try:
        metadata = verify(arguments.export_directory)
    except (VerificationError, OSError, ValueError, TypeError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {metadata['export_id']}: {metadata['counts']['payload']} payload entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
