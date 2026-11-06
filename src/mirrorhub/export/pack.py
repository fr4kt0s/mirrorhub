"""Archive creation, optional zstd compression and optional splitting."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from mirrorhub.export.manifest import ExportError, Inventory, Pulse, file_hash


def archive(
    root: Path,
    output: Path,
    inventory: Inventory,
    changed: list[str],
    pulse: Pulse,
) -> None:
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as tar:
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
                if file_hash(path, pulse) != entry["sha256"]:
                    raise ExportError(f"file changed before packing: {relative}")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as handle:
                    status = os.fstat(handle.fileno())
                    if status.st_size != entry["size"] or status.st_mtime_ns != entry["mtime_ns"]:
                        raise ExportError(f"file changed before packing: {relative}")
                    info.size = entry["size"]
                    info.mode = 0o644
                    tar.addfile(info, handle)

            pulse(
                20 + 50 * (index + 1) / max(len(changed), 1),
                f"Packed {index + 1}/{len(changed)} entries",
            )


def compress(source: Path, destination: Path, pulse: Pulse) -> None:
    executable = shutil.which("zstd")
    if executable is None:
        raise ExportError("zstd is not installed")

    with destination.open("xb") as output:
        process = subprocess.Popen(  # noqa: S603
            [executable, "-q", "-T1", "-c", str(source)],
            stdout=output,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        try:
            while True:
                try:
                    result = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pulse(75, "Compressing archive")
            if result != 0:
                raise ExportError(f"zstd exited {result}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def split(source: Path, part_bytes: int, pulse: Pulse) -> list[Path]:
    if part_bytes <= 0:
        return [source]

    parts: list[Path] = []
    with source.open("rb") as handle:
        index = 0
        while True:
            first = handle.read(min(1024 * 1024, part_bytes))
            if not first:
                break
            destination = source.with_name(f"{source.name}.{index:05d}")
            with destination.open("xb") as output:
                output.write(first)
                written = len(first)
                while written < part_bytes:
                    chunk = handle.read(min(1024 * 1024, part_bytes - written))
                    if not chunk:
                        break
                    output.write(chunk)
                    written += len(chunk)
                    pulse(80, f"Writing archive part {index + 1}")
            parts.append(destination)
            index += 1

    source.unlink()
    return parts
