"""Safe streaming extraction of the export archive into a staging directory."""

from __future__ import annotations

import hashlib
import os
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from mirrorhub.importer.checks import Export, safe_link, safe_relative
from mirrorhub.importer.errors import UnsafeArchiveError

CHUNK = 1024 * 1024


class _Directories:
    """mkdir for staging parents, each directory only once per extraction.

    Staging is created empty by the importer and nothing else writes to it, so a
    directory created (or found) once stays a directory.
    """

    def __init__(self) -> None:
        self._known: set[Path] = set()

    def ensure(self, directory: Path) -> None:
        if directory not in self._known:
            directory.mkdir(parents=True, exist_ok=True)
            self._known.add(directory)


def _write_file(
    tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path, entry: Mapping[str, Any]
) -> None:
    """Write one member and compare size and SHA-256 with the inventory.

    No fsync here: the importer syncs once after the whole archive is staged and
    before it writes the journal (apply._apply_locked), which gives the same crash
    guarantee without one disk flush per file.
    """
    source = tar.extractfile(member)
    if source is None:
        raise UnsafeArchiveError(f"cannot read member data: {member.name!r}")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "wb") as out:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        os.fchmod(out.fileno(), 0o644)  # independent of the umask
    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
        raise UnsafeArchiveError(f"content does not match inventory: {member.name!r}")
    os.utime(dest, ns=(entry["mtime_ns"], entry["mtime_ns"]))


def extract_payload(export: Export, stream: IO[bytes], staging: Path) -> None:
    """Stream the tar; accept only the regular files and symlinks promised by the manifest.

    The tar is never passed to extractall; every member is checked and written by hand.
    """
    seen: set[str] = set()
    directories = _Directories()
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            name = member.name
            if not safe_relative(name):
                raise UnsafeArchiveError(f"unsafe member name: {name!r}")
            if name in seen:
                raise UnsafeArchiveError(f"duplicate member: {name!r}")
            if name not in export.payload:
                raise UnsafeArchiveError(f"member not listed in payload: {name!r}")
            entry = export.inventory[name]
            seen.add(name)
            dest = staging / name
            if member.type == tarfile.SYMTYPE:
                if (
                    entry["type"] != "symlink"
                    or member.linkname != entry["target"]
                    or not safe_link(name, member.linkname)
                ):
                    raise UnsafeArchiveError(f"symlink does not match inventory: {name!r}")
                directories.ensure(dest.parent)
                os.symlink(member.linkname, dest)
                os.utime(dest, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)
            elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                if entry["type"] != "file" or member.size != entry["size"]:
                    raise UnsafeArchiveError(f"file does not match inventory: {name!r}")
                directories.ensure(dest.parent)
                _write_file(tar, member, dest, entry)
            else:
                raise UnsafeArchiveError(f"unsupported member type for {name!r}: {member.type!r}")
    missing = export.payload - seen
    if missing:
        raise UnsafeArchiveError(f"archive is missing {len(missing)} payload members")
