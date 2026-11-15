"""Standalone verification for a published MirrorHub export."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from mirrorhub.export.manifest import load as load_inventory

FORMAT_VERSION = 1
CHECKSUM_EXCLUSIONS = frozenset({"MANIFEST.sha256", "MANIFEST.sha256.sig", ".done"})


class VerificationError(RuntimeError):
    """An export failed verification."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_export(directory: Path) -> Path:
    """Return the newest published export below a repository root.

    If *directory* itself contains .done, it is already an export directory.
    Otherwise, search recursively for published exports and select the newest
    candidate by modification time of its .done marker.
    """
    if (directory / ".done").is_file():
        return directory

    candidates = [
        marker.parent
        for marker in directory.rglob(".done")
        if marker.is_file()
    ]

    if not candidates:
        raise VerificationError(
            f"no published export with .done found below: {directory}"
        )

    return max(candidates, key=lambda path: (path / ".done").stat().st_mtime)


def verify(directory: Path | str) -> dict[str, Any]:
    """Verify the newest published export below a path without modifying it.

    *directory* may be either a single export directory or a repository export
    root. Returns the export metadata on success. Raises VerificationError when
    the export is incomplete, inconsistent, or contains a checksum mismatch.
    """
    root = Path(directory)
    if not root.is_dir():
        raise VerificationError(f"export directory does not exist: {root}")

    export = _resolve_export(root)

    done = export / ".done"
    if not done.is_file():
        raise VerificationError("export is not published: .done is missing")

    metadata_path = export / "manifest.json"
    if not metadata_path.is_file():
        raise VerificationError("manifest.json is missing")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"manifest.json is invalid: {exc}") from exc

    if not isinstance(metadata, dict):
        raise VerificationError("manifest.json must contain an object")

    required = {
        "format_version",
        "export_id",
        "repo",
        "mode",
        "created_at",
        "archive_format",
        "archive_parts",
    }
    missing = sorted(required - set(metadata))
    if missing:
        raise VerificationError(f"manifest.json is missing fields: {', '.join(missing)}")

    if metadata["format_version"] != FORMAT_VERSION:
        raise VerificationError(
            f"unsupported export format version: {metadata['format_version']}"
        )

    expected_export_id = done.read_text(encoding="ascii").strip()
    if expected_export_id != metadata["export_id"]:
        raise VerificationError(
            f"export identity mismatch: .done={expected_export_id!r}, "
            f"manifest={metadata['export_id']!r}"
        )

    archive_parts = metadata["archive_parts"]
    if not isinstance(archive_parts, list) or not all(
        isinstance(item, str) for item in archive_parts
    ):
        raise VerificationError("manifest archive_parts must be a list of strings")

    for name in archive_parts:
        path = export / name
        if not path.is_file():
            raise VerificationError(f"archive part is missing: {name}")

    checksum_path = export / "MANIFEST.sha256"
    if not checksum_path.is_file():
        raise VerificationError("MANIFEST.sha256 is missing")

    checked = 0
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="ascii").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue

        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[1]:
            raise VerificationError(
                f"invalid checksum line {line_number} in MANIFEST.sha256"
            )

        expected_digest, name = parts
        name = name.strip()
        if name in CHECKSUM_EXCLUSIONS:
            raise VerificationError(
                f"checksum manifest must not contain {name}"
            )

        path = export / name
        if not path.is_file():
            raise VerificationError(f"checksummed file is missing: {name}")

        actual_digest = _sha256(path)
        if actual_digest != expected_digest.lower():
            raise VerificationError(
                f"checksum mismatch for {name}: "
                f"expected {expected_digest}, got {actual_digest}"
            )
        checked += 1

    if checked == 0:
        raise VerificationError("MANIFEST.sha256 contains no checksum entries")

    inventory_path = export / "files.json"
    if not inventory_path.is_file():
        raise VerificationError("files.json is missing")

    try:
        inventory = load_inventory(inventory_path)
    except Exception as exc:
        raise VerificationError(f"files.json is invalid: {exc}") from exc

    if len(inventory) != metadata.get("counts", {}).get("inventory"):
        raise VerificationError(
            "files.json inventory count does not match manifest metadata"
        )

    deleted_path = export / "deleted.txt"
    if not deleted_path.is_file():
        raise VerificationError("deleted.txt is missing")

    metadata["verified_path"] = str(export)
    return metadata


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m mirrorhub.verify <export-or-repository-directory>", file=sys.stderr)
        return 2

    try:
        metadata = verify(sys.argv[1])
    except VerificationError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "OK: export verified "
        f"({metadata['repo']} {metadata['export_id']} "
        f"mode={metadata['mode']})\n"
        f"verified: {metadata['verified_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
