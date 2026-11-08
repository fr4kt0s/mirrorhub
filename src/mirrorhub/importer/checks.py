"""Receiver-side checks on top of mirrorhub.verify (which stays unchanged)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mirrorhub.importer.errors import (
    ConfigError,
    SignatureError,
    ToolMissingError,
    VerificationFailed,
)
from mirrorhub.importer.logs import LOGGER
from mirrorhub.verify import VerificationError, verify

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-(full|incr)$")
SKIP_DIRECTORIES = frozenset({".temp", ".tmp", ".mirrorhub", ".rsync-partial"})

Inventory = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Export:
    directory: Path
    slug: str
    export_id: str
    mode: str
    base_export_id: str | None
    parts: tuple[Path, ...]
    part_digests: tuple[str, ...]
    compressed: bool
    inventory: Inventory
    payload: frozenset[str]
    deleted: tuple[str, ...]


def safe_relative(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
        and all(ord(character) >= 32 for character in value)
    )


def safe_link(path: str, target: object) -> bool:
    if (
        not isinstance(target, str)
        or not target
        or target.startswith("/")
        or "\\" in target
        or any(ord(character) < 32 for character in target)
    ):
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved != ".." and not resolved.startswith("../")


def _expected_parts(archive_format: object, names: list[str]) -> tuple[list[str], bool]:
    if archive_format not in ("tar", "tar.zst"):
        raise VerificationFailed(f"unsupported archive_format: {archive_format!r}")
    base = f"data.{archive_format}"
    if names == [base]:
        return names, archive_format == "tar.zst"
    expected = [f"{base}.{index:05d}" for index in range(len(names))]
    if sorted(names) != expected:
        raise VerificationFailed("archive parts are not a contiguous numbered sequence")
    return expected, archive_format == "tar.zst"


def load_export(directory: Path) -> Export:
    """Run the unmodified verifier, then validate what it does not check."""
    try:
        metadata = verify(directory)
        inventory: Inventory = json.loads((directory / "files.json").read_text(encoding="utf-8"))
        deleted = tuple((directory / "deleted.txt").read_text(encoding="utf-8").splitlines())
        digests: dict[str, str] = {}
        for line in (directory / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
            digest, _, name = line.partition("  ")
            digests[name] = digest
    except (VerificationError, OSError, ValueError, TypeError, KeyError) as exc:
        raise VerificationFailed(f"verification failed: {exc}") from exc

    slug = directory.parent.name
    export_id = str(metadata["export_id"])
    if not SLUG_RE.fullmatch(slug):
        raise VerificationFailed(f"invalid repository slug: {slug!r}")
    match = EXPORT_ID_RE.fullmatch(export_id)
    if directory.name != export_id or match is None:
        raise VerificationFailed("export directory name does not match export_id")
    if match.group(1) != metadata["mode"]:
        raise VerificationFailed("export_id suffix does not match mode")
    if metadata.get("repo") != slug:
        raise VerificationFailed("manifest repo does not match directory slug")

    part_names, compressed = _expected_parts(
        metadata.get("archive_format"), list(metadata["archive_parts"])
    )

    for relative, entry in inventory.items():
        if entry["type"] == "symlink":
            target = entry.get("target")
            if not safe_link(relative, target):
                raise VerificationFailed(f"unsafe symlink target: {relative!r}")
            assert isinstance(target, str)
            if hashlib.sha256(target.encode("utf-8")).hexdigest() != entry["sha256"]:
                raise VerificationFailed(f"symlink digest mismatch: {relative!r}")
        parent = posixpath.dirname(relative)
        while parent:
            if parent in inventory:
                raise VerificationFailed(f"inventory path conflict: {relative!r} under {parent!r}")
            parent = posixpath.dirname(parent)

    return Export(
        directory=directory,
        slug=slug,
        export_id=export_id,
        mode=str(metadata["mode"]),
        base_export_id=metadata.get("base_export_id"),
        parts=tuple(directory / name for name in part_names),
        part_digests=tuple(digests[name] for name in part_names),
        compressed=compressed,
        inventory=inventory,
        payload=frozenset(metadata["payload_paths"]),
        deleted=deleted,
    )


def check_signature(directory: Path, keyring: Path | None, require: bool) -> None:
    """Verify MANIFEST.sha256.sig against an independently provisioned keyring."""
    signature = directory / "MANIFEST.sha256.sig"
    if keyring is None:
        if require:
            raise ConfigError("require_signature is set but no keyring is configured")
        if signature.exists():
            LOGGER.warning("signature present but not checked (no keyring configured)")
        return
    if not signature.is_file():
        if require:
            raise SignatureError("export has no MANIFEST.sha256.sig")
        LOGGER.warning("export is unsigned")
        return
    gpgv = shutil.which("gpgv")
    if gpgv is None:
        raise ToolMissingError("gpgv is not installed")
    result = subprocess.run(  # noqa: S603
        [gpgv, "--keyring", str(keyring), str(signature), str(directory / "MANIFEST.sha256")],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SignatureError(f"signature check failed: {detail}")
