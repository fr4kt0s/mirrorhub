"""Stream archive parts in numeric order, hashing each part, optionally through zstd."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Buffer, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any

from mirrorhub.importer.checks import Export
from mirrorhub.importer.errors import ToolMissingError, UnsafeArchiveError, VerificationFailed

CHUNK = 1024 * 1024


class PartReader(io.RawIOBase):
    """Concatenate parts and compare each part's SHA-256 with MANIFEST.sha256 at its end."""

    def __init__(self, parts: tuple[Path, ...], digests: tuple[str, ...]) -> None:
        super().__init__()
        self._parts = parts
        self._digests = digests
        self._index = 0
        self._handle: IO[bytes] | None = None
        self._hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        while True:
            if self._handle is None:
                if self._index >= len(self._parts):
                    return 0
                self._handle = self._parts[self._index].open("rb")
                self._hash = hashlib.sha256()
            chunk = self._handle.read(len(view))
            if chunk:
                self._hash.update(chunk)
                view[: len(chunk)] = chunk
                return len(chunk)
            self._handle.close()
            self._handle = None
            if self._hash.hexdigest() != self._digests[self._index]:
                name = self._parts[self._index].name
                raise VerificationFailed(f"archive part changed after verification: {name}")
            self._index += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        super().close()


def _drain(stream: IO[bytes]) -> None:
    while stream.read(CHUNK):
        pass


@contextmanager
def open_archive(export: Export) -> Iterator[IO[bytes]]:
    """Yield the uncompressed tar stream; after use all parts are fully read and re-verified."""
    reader = io.BufferedReader(PartReader(export.parts, export.part_digests), CHUNK)
    if not export.compressed:
        try:
            yield reader
            _drain(reader)
        finally:
            reader.close()
        return

    zstd = shutil.which("zstd")
    if zstd is None:
        reader.close()
        raise ToolMissingError("zstd is not installed; it is required for .tar.zst exports")

    errors: list[BaseException] = []
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(  # noqa: S603
            [zstd, "-d", "-q", "-c"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            shell=False,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        sink: Any = process.stdin

        def feed() -> None:
            try:
                with suppress(BrokenPipeError):
                    shutil.copyfileobj(reader, sink, CHUNK)
            except BaseException as exc:
                errors.append(exc)
            finally:
                with suppress(OSError):
                    sink.close()

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        failure: BaseException | None = None
        try:
            yield process.stdout
            _drain(process.stdout)
        except BaseException as exc:
            failure = exc
            process.kill()
        finally:
            thread.join()
            process.stdout.close()
            code = process.wait()
            reader.close()
        if errors:
            raise errors[0]
        if failure is not None:
            raise failure
        if code != 0:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", "replace").strip()
            raise UnsafeArchiveError(f"zstd failed with exit code {code}: {detail}")
