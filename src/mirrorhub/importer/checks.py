"""Receiver-side checks on top of mirrorhub.verify (which stays unchanged)."""

from __future__ import annotations

import hashlib
import json
import logging
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
from mirrorhub.importer.logs import log_event
from mirrorhub.verify import CHECKSUM_EXCLUSIONS, FORMAT_VERSION, VerificationError, verify

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z-(full|incr)$")
SKIP_DIRECTORIES = frozenset({".temp", ".tmp", ".mirrorhub", ".rsync-partial"})
REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "format_version",
        "export_id",
        "repo",
        "mode",
        "created_at",
        "archive_format",
        "archive_parts",
    }
)

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
    sender_commit: str | None = None
    # manifest field of a repair export (#43): the request it answers; None otherwise
    repair: dict[str, Any] | None = None


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


def _read_checksums(directory: Path) -> dict[str, str]:
    """MANIFEST.sha256 as {name: lower-case digest}, with the verifier's line rules."""
    digests: dict[str, str] = {}
    text = (directory / "MANIFEST.sha256").read_text(encoding="ascii")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            raise VerificationFailed(f"invalid checksum line {line_number} in MANIFEST.sha256")
        name = parts[1].strip()
        if name in CHECKSUM_EXCLUSIONS:
            raise VerificationFailed(f"checksum manifest must not contain {name}")
        digests[name] = parts[0].lower()
    if not digests:
        raise VerificationFailed("MANIFEST.sha256 contains no checksum entries")
    return digests


def _verify_without_parts(directory: Path) -> tuple[dict[str, Any], Inventory]:
    """The checks of mirrorhub.verify.verify() except hashing the archive parts.

    The parts must exist and be listed in MANIFEST.sha256; their digests are compared
    while the archive is streamed (reader.PartReader), so a large export is read once
    instead of twice. Every other checksummed file (files.json, manifest.json,
    deleted.txt, ...) is hashed here, before anything is extracted.
    """
    done = directory / ".done"
    if not done.is_file():
        raise VerificationFailed("export is not published: .done is missing")
    try:
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationFailed("manifest.json is missing") from exc
    except ValueError as exc:
        raise VerificationFailed(f"manifest.json is invalid: {exc}") from exc
    if not isinstance(metadata, dict):
        raise VerificationFailed("manifest.json must contain an object")
    missing = sorted(REQUIRED_MANIFEST_FIELDS - set(metadata))
    if missing:
        raise VerificationFailed(f"manifest.json is missing fields: {', '.join(missing)}")
    if metadata["format_version"] != FORMAT_VERSION:
        raise VerificationFailed(f"unsupported export format version: {metadata['format_version']}")
    marker = done.read_text(encoding="ascii").strip()
    if marker != metadata["export_id"]:
        raise VerificationFailed(
            f"export identity mismatch: .done={marker!r}, manifest={metadata['export_id']!r}"
        )
    parts = metadata["archive_parts"]
    if not isinstance(parts, list) or not all(isinstance(item, str) for item in parts):
        raise VerificationFailed("manifest archive_parts must be a list of strings")
    for name in parts:
        if not (directory / name).is_file():
            raise VerificationFailed(f"archive part is missing: {name}")

    digests = _read_checksums(directory)
    archive = set(parts)
    for name, expected in digests.items():
        path = directory / name
        if not path.is_file():
            raise VerificationFailed(f"checksummed file is missing: {name}")
        if name in archive:
            continue  # compared while streaming
        if _sha256_file(path) != expected:
            raise VerificationFailed(f"checksum mismatch for {name}")

    if not (directory / "files.json").is_file():
        raise VerificationFailed("files.json is missing")
    try:
        inventory = json.loads((directory / "files.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        raise VerificationFailed(f"files.json is invalid: {exc}") from exc
    if not isinstance(inventory, dict):
        raise VerificationFailed("files.json is invalid: inventory must be a mapping")
    for relative, entry in inventory.items():
        if not safe_relative(relative):
            raise VerificationFailed(f"files.json is invalid: unsafe relative path: {relative!r}")
        if not isinstance(entry, dict):
            raise VerificationFailed("files.json is invalid: invalid inventory entry")
    if len(inventory) != (metadata.get("counts") or {}).get("inventory"):
        raise VerificationFailed("files.json inventory count does not match manifest metadata")
    if not (directory / "deleted.txt").is_file():
        raise VerificationFailed("deleted.txt is missing")
    return metadata, inventory


def verify_parts(export: Export) -> None:
    """Hash every archive part against MANIFEST.sha256 (reads the whole archive).

    Used after a failed extraction to tell a damaged archive (exit code 3) from an
    export whose archive does not match its own inventory.
    """
    for path, expected in zip(export.parts, export.part_digests, strict=True):
        if _sha256_file(path) != expected:
            raise VerificationFailed(f"checksum mismatch for archive part {path.name}")


def load_export(directory: Path, *, verify_parts_first: bool = False) -> Export:
    """Check an export before anything is extracted.

    By default the archive parts are only required to exist and be listed in
    MANIFEST.sha256; they are hashed while streaming. With verify_parts_first the
    unmodified mirrorhub.verify.verify() runs first and hashes them up front too.
    """
    try:
        if verify_parts_first:
            metadata = verify(directory)
            inventory: Inventory = json.loads(
                (directory / "files.json").read_text(encoding="utf-8")
            )
        else:
            metadata, inventory = _verify_without_parts(directory)
        deleted = tuple((directory / "deleted.txt").read_text(encoding="utf-8").splitlines())
        digests = _read_checksums(directory)
    except VerificationFailed:
        raise
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
    unlisted = [name for name in part_names if name not in digests]
    if unlisted:
        raise VerificationFailed(f"archive part not covered by MANIFEST.sha256: {unlisted[0]}")
    _check_inventory(inventory)
    repair = metadata.get("repair")
    if repair is not None and (not isinstance(repair, dict) or metadata["mode"] != "incr"):
        raise VerificationFailed("manifest field repair must be an object of an incremental")
    log_event(
        logging.DEBUG,
        "export verified",
        slug=slug,
        export_id=export_id,
        mode=metadata["mode"],
        base_export_id=metadata.get("base_export_id"),
        archive_format=metadata.get("archive_format"),
        parts=part_names,
        inventory=len(inventory),
        payload=len(metadata["payload_paths"]),
        deleted=len(deleted),
        parts_hashed="up front" if verify_parts_first else "while streaming",
        repair=repair is not None,
    )

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
        sender_commit=_sender_commit(metadata),
        repair=repair,
    )


def _sender_commit(metadata: dict[str, Any]) -> str | None:
    versions = metadata.get("tool_versions")
    value = versions.get("mirrorhub_commit") if isinstance(versions, dict) else None
    return value if isinstance(value, str) and value else None


def _check_inventory(inventory: Inventory) -> None:
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


@dataclass(frozen=True)
class ExportMetadata:
    """Identity and inventory of an export, checked without its archive."""

    directory: Path
    slug: str
    export_id: str
    mode: str
    base_export_id: str | None
    inventory: Inventory


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_metadata(directory: Path) -> ExportMetadata:
    """Check files.json and manifest.json against MANIFEST.sha256; archive parts are
    neither required nor hashed. The detached signature, if checked, covers
    MANIFEST.sha256 and therefore these two files."""
    try:
        done = (directory / ".done").read_text(encoding="ascii").strip()
        digests: dict[str, str] = {}
        for line in (directory / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
            digest, _, name = line.strip().partition("  ")
            if name:
                digests[name.strip()] = digest.lower()
        for name in ("files.json", "manifest.json"):
            if name not in digests:
                raise VerificationFailed(f"{name} is not covered by MANIFEST.sha256")
            if _sha256_file(directory / name) != digests[name]:
                raise VerificationFailed(f"checksum mismatch for {name}")
        metadata: dict[str, Any] = json.loads((directory / "manifest.json").read_text("utf-8"))
        inventory: Inventory = json.loads((directory / "files.json").read_text("utf-8"))
    except VerificationFailed:
        raise
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise VerificationFailed(f"verification failed: {exc}") from exc

    slug = directory.parent.name
    export_id = str(metadata.get("export_id"))
    match = EXPORT_ID_RE.fullmatch(export_id)
    if not SLUG_RE.fullmatch(slug):
        raise VerificationFailed(f"invalid repository slug: {slug!r}")
    if metadata.get("format_version") != 1:
        raise VerificationFailed("unsupported export format version")
    if directory.name != export_id or match is None or done != export_id:
        raise VerificationFailed("export directory name, .done and export_id do not match")
    if match.group(1) != metadata.get("mode"):
        raise VerificationFailed("export_id suffix does not match mode")
    if metadata.get("repo") != slug:
        raise VerificationFailed("manifest repo does not match directory slug")
    if not isinstance(inventory, dict):
        raise VerificationFailed("files.json must contain an object")
    expected = (metadata.get("counts") or {}).get("inventory")
    if expected is not None and expected != len(inventory):
        raise VerificationFailed("inventory count does not match manifest")
    try:
        for relative, entry in inventory.items():
            if (
                not safe_relative(relative)
                or entry["type"] not in ("file", "symlink")
                or not isinstance(entry["size"], int)
                or not isinstance(entry["mtime_ns"], int)
                or not isinstance(entry["sha256"], str)
            ):
                raise VerificationFailed(f"invalid inventory entry: {relative!r}")
        _check_inventory(inventory)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VerificationFailed(f"invalid inventory: {exc}") from exc

    base = metadata.get("base_export_id")
    log_event(
        logging.DEBUG,
        "export metadata verified",
        slug=slug,
        export_id=export_id,
        mode=metadata.get("mode"),
        base_export_id=base,
        inventory=len(inventory),
    )
    return ExportMetadata(
        directory=directory,
        slug=slug,
        export_id=export_id,
        mode=str(metadata["mode"]),
        base_export_id=base if isinstance(base, str) and base else None,
        inventory=inventory,
    )


def check_signature(directory: Path, keyring: Path | None, require: bool) -> None:
    """Verify MANIFEST.sha256.sig against an independently provisioned keyring."""
    signature = directory / "MANIFEST.sha256.sig"
    where = {"slug": directory.parent.name, "export_id": directory.name}
    if keyring is None:
        if require:
            raise ConfigError("require_signature is set but no keyring is configured")
        if signature.exists():
            log_event(
                logging.WARNING,
                "signature present but not checked (no keyring configured)",
                **where,
            )
        else:
            log_event(logging.DEBUG, "signature not checked (no keyring configured)", **where)
        return
    if not signature.is_file():
        if require:
            raise SignatureError("export has no MANIFEST.sha256.sig")
        log_event(logging.WARNING, "export is unsigned", **where)
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
    log_event(logging.DEBUG, "signature verified", keyring=keyring, **where)
