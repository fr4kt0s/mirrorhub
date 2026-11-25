"""Write the export archive in one pass: tar, optional zstd, optional parts.

Every payload file is read exactly once. While tarfile copies it into the archive,
the bytes are hashed; the digest is compared with the inventory (incremental export)
or becomes the inventory's digest (full export, whose inventory is built without
reading file contents). The archive bytes go straight to the part files, or through
``zstd`` first, and each part is hashed as it is written, so ``MANIFEST.sha256`` does
not have to read the archive again. No intermediate ``data.tar`` is written.

The tar stream is byte-for-byte what the previous implementation produced (same
TarInfo fields, PAX format, same part names and sizes). zstd now uses all cores
(``-T0``); its frames may be laid out differently, the decompressed tar is the same.
Receivers are unaffected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import IO, Any

from mirrorhub.export.manifest import ExportError, Inventory, Pulse

COPY_BUFFER = 1024 * 1024
PROGRESS_BYTES = 64 * 1024 * 1024


class PartWriter:
    """File-like sink that writes ``name`` or ``name.00000``, ``name.00001`` … parts.

    Each part is hashed while it is written. ``part_bytes`` <= 0 writes one file.
    """

    def __init__(self, directory: Path, name: str, part_bytes: int) -> None:
        self.directory = directory
        self.name = name
        self.part_bytes = part_bytes
        self.parts: list[tuple[Path, str]] = []
        self._handle: IO[bytes] | None = None
        self._digest = hashlib.sha256()
        self._written = 0
        self._path: Path | None = None

    def _open_next(self) -> None:
        index = len(self.parts)
        name = self.name if self.part_bytes <= 0 else f"{self.name}.{index:05d}"
        self._path = self.directory / name
        self._handle = self._path.open("xb")
        self._digest = hashlib.sha256()
        self._written = 0

    def _finish_current(self) -> None:
        if self._handle is None or self._path is None:
            return
        self._handle.close()
        self.parts.append((self._path, self._digest.hexdigest()))
        self._handle = None
        self._path = None

    def write(self, data: bytes | bytearray | memoryview) -> int:
        view = memoryview(data)
        total = len(view)
        while view:
            if self._handle is None:
                self._open_next()
            assert self._handle is not None
            room = len(view) if self.part_bytes <= 0 else self.part_bytes - self._written
            chunk = view[:room]
            self._handle.write(chunk)
            self._digest.update(chunk)
            self._written += len(chunk)
            view = view[len(chunk) :]
            if self.part_bytes > 0 and self._written >= self.part_bytes:
                self._finish_current()
        return total

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> list[tuple[Path, str]]:
        """Finish writing; returns (path, sha256) of every part in order."""
        self._finish_current()
        if not self.parts and self.part_bytes <= 0:
            # Nothing written at all: keep the single-file layout (an empty file).
            self._open_next()
            self._finish_current()
        return self.parts

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class _HashingReader:
    """Wraps a file: hashes what tarfile reads, reports progress every 64 MiB."""

    def __init__(self, handle: IO[bytes], label: str, pulse: Pulse) -> None:
        self.handle = handle
        self.label = label
        self.pulse = pulse
        self.digest = hashlib.sha256()
        self.count = 0
        self._next_report = PROGRESS_BYTES

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.digest.update(data)
        self.count += len(data)
        if self.count >= self._next_report:
            self._next_report += PROGRESS_BYTES
            self.pulse(None, f"Packing {self.label} ({self.count // (1024 * 1024)} MiB)")
        return data


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _add_entries(
    tar: tarfile.TarFile,
    root: Path,
    inventory: Inventory,
    changed: list[str],
    pulse: Pulse,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for index, relative in enumerate(changed):
        path = root / relative
        entry = inventory[relative]
        info = tarfile.TarInfo(relative)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = entry["mtime_ns"] // 1_000_000_000

        if entry["type"] == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry["target"]:
                raise ExportError(f"symlink changed: {relative}")
            info.type = tarfile.SYMTYPE
            info.linkname = entry["target"]
            info.mode = 0o777
            tar.addfile(info)
        else:
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                raise ExportError(f"file changed before packing: {relative}") from exc
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if before.st_size != entry["size"] or before.st_mtime_ns != entry["mtime_ns"]:
                    raise ExportError(f"file changed before packing: {relative}")
                info.size = entry["size"]
                info.mode = 0o644
                reader = _HashingReader(handle, path.name, pulse)
                try:
                    tar.addfile(info, reader)
                except OSError as exc:
                    # tarfile raises when the file is shorter than its size.
                    raise ExportError(f"file changed while packing: {relative}") from exc
                after = os.fstat(handle.fileno())
            if _identity(before) != _identity(after) or reader.count != entry["size"]:
                raise ExportError(f"file changed while packing: {relative}")
            digest = reader.digest.hexdigest()
            if entry["sha256"] and digest != entry["sha256"]:
                raise ExportError(f"file changed before packing: {relative}")
            digests[relative] = digest

        pulse(
            20 + 60 * (index + 1) / max(len(changed), 1),
            f"Packed {index + 1}/{len(changed)} entries",
        )
    return digests


def write_archive(
    root: Path,
    directory: Path,
    inventory: Inventory,
    changed: list[str],
    pulse: Pulse,
    *,
    compress: bool,
    part_bytes: int,
) -> tuple[list[tuple[Path, str]], dict[str, str]]:
    """Write ``data.tar`` / ``data.tar.zst`` (or its parts) into ``directory``.

    Returns the parts as (path, sha256) and the SHA-256 of every packed regular file.
    """
    sink = PartWriter(directory, "data.tar.zst" if compress else "data.tar", part_bytes)
    process: subprocess.Popen[bytes] | None = None
    copier: threading.Thread | None = None
    copy_error: list[BaseException] = []

    try:
        if compress:
            executable = shutil.which("zstd")
            if executable is None:
                raise ExportError("zstd is not installed")
            process = subprocess.Popen(  # noqa: S603
                [executable, "-q", "-T0", "-c"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            assert process.stdout is not None and process.stdin is not None
            output = process.stdout

            def copy() -> None:
                try:
                    while chunk := output.read(COPY_BUFFER):
                        sink.write(chunk)
                except BaseException as exc:  # reported by the main thread
                    copy_error.append(exc)

            copier = threading.Thread(target=copy, name="zstd-output", daemon=True)
            copier.start()
            target: Any = process.stdin
        else:
            target = sink

        tar = tarfile.open(  # noqa: SIM115 - closed below, also on errors
            fileobj=target,
            mode="w|",
            format=tarfile.PAX_FORMAT,
        )
        # Copy 1 MiB at a time instead of 16 KiB (TarFile(copybufsize=…), not in typeshed).
        setattr(tar, "copybufsize", COPY_BUFFER)  # noqa: B010
        try:
            digests = _add_entries(tar, root, inventory, changed, pulse)
        finally:
            try:
                tar.close()
            except OSError as exc:
                if not copy_error:
                    raise ExportError(f"cannot write archive: {exc}") from exc

        if process is not None:
            assert process.stdin is not None and copier is not None
            pulse(82, "Finishing compression")
            process.stdin.close()
            copier.join()
            result = process.wait()
            if copy_error:
                raise ExportError(f"cannot write archive: {copy_error[0]}")
            if result != 0:
                raise ExportError(f"zstd exited {result}")
        return sink.close(), digests
    except BaseException:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if copier is not None:
                copier.join(timeout=5)
        sink.abort()
        raise
