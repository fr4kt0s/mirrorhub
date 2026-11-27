"""Progress lines for long phases of an import or check.

Every `interval` seconds a phase logs one JSON line (event "progress", level info)
with files and bytes done, totals, percent, rate and estimated time left. A phase
that logged at least one progress line ends with "phase done" at info level, a
short one only at debug level, so small imports stay quiet.

    {"event": "progress", "phase": "staging", "files": 120000, "files_total": 480000,
     "bytes": 51539607552, "bytes_total": 214748364800, "percent": 24.0,
     "rate_mib_s": 81.9, "elapsed_s": 600, "eta_s": 1900, ...}

The interval is set once per run (--progress-interval, default 30 s, 0 = off).
"""

from __future__ import annotations

import logging
import time

from mirrorhub.importer.logs import log_event

DEFAULT_INTERVAL = 30.0
_interval = DEFAULT_INTERVAL
_clock = time.monotonic

MIB = 1024 * 1024


def configure(interval: float) -> None:
    """Seconds between progress lines; 0 turns them off."""
    global _interval
    if interval < 0:
        raise ValueError("progress interval must not be negative")
    _interval = interval


def interval() -> float:
    """Return the progress interval in seconds (0 disables progress lines)."""
    return _interval


class Progress:
    """Counts files and bytes of one phase and logs at most once per interval."""

    def __init__(
        self,
        phase: str,
        *,
        files_total: int | None = None,
        bytes_total: int | None = None,
        slug: str | None = None,
        export_id: str | None = None,
    ) -> None:
        self.phase = phase
        self.files_total = files_total
        self.bytes_total = bytes_total
        self.fields = {
            key: value
            for key, value in (("slug", slug), ("export_id", export_id))
            if value is not None
        }
        self.files = 0
        self.bytes = 0
        self.reported = False
        self.started = _clock()
        self._next = self.started + _interval

    def advance(self, files: int = 1, nbytes: int = 0) -> None:
        """Count files and bytes; log a progress line once per interval."""
        self.files += files
        self.bytes += nbytes
        if _interval <= 0:
            return
        now = _clock()
        if now >= self._next:
            self._log(now)
            self._next = now + _interval

    def _fraction(self) -> float | None:
        if self.bytes_total:
            return min(1.0, self.bytes / self.bytes_total)
        if self.files_total:
            return min(1.0, self.files / self.files_total)
        return None

    def _log(self, now: float) -> None:
        elapsed = now - self.started
        values: dict[str, object] = {
            "phase": self.phase,
            "files": self.files,
            "elapsed_s": round(elapsed),
        }
        if self.files_total is not None:
            values["files_total"] = self.files_total
        if self.bytes_total:
            values["bytes"] = self.bytes
            values["bytes_total"] = self.bytes_total
            if elapsed > 0:
                values["rate_mib_s"] = round(self.bytes / MIB / elapsed, 1)
        fraction = self._fraction()
        if fraction is not None:
            values["percent"] = round(fraction * 100, 1)
            if fraction > 0:
                values["eta_s"] = round(elapsed * (1 - fraction) / fraction)
        log_event(logging.INFO, "progress", **values, **self.fields)
        self.reported = True

    def finish(self) -> None:
        """Log the phase's totals (``phase done``): info if it reported progress, else debug."""
        elapsed = _clock() - self.started
        values: dict[str, object] = {
            "phase": self.phase,
            "files": self.files,
            "elapsed_s": round(elapsed, 1),
        }
        if self.bytes:
            values["bytes"] = self.bytes
            if elapsed > 0:
                values["rate_mib_s"] = round(self.bytes / MIB / elapsed, 1)
        level = logging.INFO if self.reported else logging.DEBUG
        log_event(level, "phase done", **values, **self.fields)
