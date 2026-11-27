"""Copy log files a tool writes itself into the job log (like tail -F).

ftpsync writes its own protocol and the rsync output to files in its LOGDIR, not to
stdout, so the job log showed little more than the command and the exit code. The
runner follows those files while the step runs and writes their new lines, with a
prefix, into the job log.

- A file that exists when the step starts belongs to an earlier run: only lines
  appended after the start are copied. A file that is replaced (ftpsync rotates its
  logs with savelog at the start of a run) is read from its beginning.
- `live_lines` limits how many lines of a file go into the job log. The rest is
  counted; at the end the closing summary (from rsync's "Number of files:") follows.
- `archive_as` moves the file next to the job log after the step
  (<job id>.<archive_as>.log), where the job page offers it for download.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mirrorhub.worker.followlog")

READ_CHUNK = 1024 * 1024  # per file and poll while the step runs
SUMMARY_MARKER = "Number of files:"  # rsync --stats
SUMMARY_MAX = 60  # lines kept for the closing summary
SUMMARY_FALLBACK = 20  # without a marker: the last lines


@dataclass(frozen=True)
class FollowedLog:
    """A log file a tool writes itself instead of stdout; the runner copies it into
    the job log while the step runs (see worker/followlog.py).

    paths: candidate names, the first one that exists is followed (names differ
    between tool versions). live_lines: how many lines go into the job log (None:
    all); beyond that the rest is counted and the closing summary is added at the
    end. archive_as: after the step the file is moved next to the job log as
    <job id>.<archive_as>.log, for download on the job page.
    """

    paths: tuple[Path, ...]
    prefix: str
    live_lines: int | None = None
    archive_as: str | None = None


def hidden_notice(prefix: str) -> str:
    return (
        f"{prefix} ... further lines are not shown here; the complete log can be "
        "downloaded on the job page"
    )


def archived_path(job_log: Path, name: str) -> Path:
    """Where a followed file is kept after the step: next to the job log."""
    return job_log.with_name(f"{job_log.stem}.{name}.log")


class _Tail:
    def __init__(self, spec: FollowedLog) -> None:
        self.spec = spec
        self.path: Path | None = None
        self.inode: int | None = None
        self.offset = 0
        self.partial = b""
        self.shown = 0
        self.hidden = 0
        self.recent: deque[str] = deque(maxlen=SUMMARY_MAX)
        # files of an earlier run: (inode, size) when the step started
        self.initial: dict[Path, tuple[int, int]] = {}
        for path in spec.paths:
            try:
                info = path.stat()
            except OSError:
                continue
            self.initial[path] = (info.st_ino, info.st_size)

    def _locate(self) -> tuple[Path, os.stat_result] | None:
        for path in self.spec.paths:
            try:
                return path, path.stat()
            except OSError:
                continue
        return None

    def read(self, *, final: bool = False) -> list[str]:
        found = self._locate()
        if found is None:
            return []
        path, info = found
        if path != self.path or info.st_ino != self.inode:  # new or rotated file
            self.path, self.inode, self.partial = path, info.st_ino, b""
            before = self.initial.get(path)
            self.offset = before[1] if before is not None and before[0] == info.st_ino else 0
        if info.st_size < self.offset:  # truncated
            self.offset, self.partial = 0, b""
        if info.st_size == self.offset and not (final and self.partial):
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read() if final else handle.read(READ_CHUNK)
        except OSError:
            return []
        self.offset += len(data)
        pieces = (self.partial + data).split(b"\n")
        self.partial = pieces.pop()
        if final and self.partial:
            pieces.append(self.partial)
            self.partial = b""
        return self._emit(piece.decode("utf-8", "replace").rstrip("\r") for piece in pieces)

    def _emit(self, lines: Iterable[str]) -> list[str]:
        out: list[str] = []
        limit = self.spec.live_lines
        for line in lines:
            if limit is None or self.shown < limit:
                out.append(f"{self.spec.prefix} {line}")
                self.shown += 1
                continue
            if self.hidden == 0:
                out.append(hidden_notice(self.spec.prefix))
            self.hidden += 1
            self.recent.append(line)
        return out

    def summary(self) -> list[str]:
        if not self.hidden:
            return []
        recent = list(self.recent)
        start = max(
            (
                index
                for index, line in enumerate(recent)
                if line.lstrip().startswith(SUMMARY_MARKER)
            ),
            default=None,
        )
        block = recent[start:] if start is not None else recent[-SUMMARY_FALLBACK:]
        prefix = self.spec.prefix
        return [f"{prefix} ... {self.hidden} lines not shown; end of the log:"] + [
            f"{prefix} {line}" for line in block
        ]

    def current_file(self) -> Path | None:
        """The file of this run, if one was written (not an untouched earlier one)."""
        if self.path is None or self.inode is None:
            return None
        before = self.initial.get(self.path)
        if before is not None and before == (self.inode, self.offset):
            return None
        return self.path


class LogFollower:
    def __init__(self, specs: tuple[FollowedLog, ...]) -> None:
        self._tails = [_Tail(spec) for spec in specs]

    def poll(self) -> list[str]:
        lines: list[str] = []
        for tail in self._tails:
            lines.extend(tail.read())
        return lines

    def finish(self) -> list[str]:
        """Everything not read yet, then the summaries of shortened files."""
        lines: list[str] = []
        for tail in self._tails:
            lines.extend(tail.read(final=True))
            lines.extend(tail.summary())
        return lines

    def archive(self, job_log: Path) -> list[Path]:
        """Move the files with archive_as next to the job log; the kept paths."""
        kept: list[Path] = []
        for tail in self._tails:
            name = tail.spec.archive_as
            source = tail.current_file()
            if name is None or source is None:
                continue
            target = archived_path(job_log, name)
            try:
                if target.exists():  # a retry of the same job: append
                    with source.open("rb") as reader, target.open("ab") as writer:
                        shutil.copyfileobj(reader, writer)
                    source.unlink()
                else:
                    try:
                        os.replace(source, target)
                    except OSError:  # another file system
                        shutil.move(source, target)
            except OSError:
                log.warning(
                    "could not keep followed log", extra={"path": str(source), "to": str(target)}
                )
                continue
            kept.append(target)
        return kept
