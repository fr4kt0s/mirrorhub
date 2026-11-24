"""Log lines on stderr: JSON for log collection, readable text for a terminal.

--log-format auto (default) writes text when stderr is a terminal and JSON lines
otherwise (systemd journal, a file, a pipe), so interactive runs are readable and
collected logs stay machine-readable. Both formats put the same fields first:
time, level, event, slug, export_id, then the rest in the order they were logged.

JSON:  {"ts": "...", "level": "info", "event": "applied", "slug": "ubuntu", ...}
text:  20:25:23 INFO    applied                ubuntu  20261123T224714Z-incr
       20:25:25 INFO    progress               ubuntu  staging  68.4 %  20524/30001 files
                        ...  40.1 MiB/58.6 MiB  20.0 MiB/s  elapsed 2s  left 1s
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, TextIO

LOGGER = logging.getLogger("mirrorhub.importer")
FORMATS = ("auto", "text", "json")
CONTEXT = ("slug", "export_id")
EVENT_WIDTH = 22


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


def _ordered(fields: Mapping[str, Any]) -> dict[str, Any]:
    """slug and export_id first, then the other fields in logging order."""
    first = {key: fields[key] for key in CONTEXT if key in fields}
    return first | {key: value for key, value in fields.items() if key not in first}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key, value in _ordered(_fields(record)).items():
            payload.setdefault(key, value)
        return json.dumps(payload, default=str)


def duration(seconds: float) -> str:
    """12s, 5m 30s, 1h 02m."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def size(count: float) -> str:
    """Bytes as MiB below 1 GiB, else GiB, one decimal."""
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GiB"
    return f"{count / 1024**2:.1f} MiB"


def _value(value: Any) -> str:
    if isinstance(value, str):
        plain = value and not any(c.isspace() or c in "\"'=" for c in value)
        return value if plain else json.dumps(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


def _progress(fields: dict[str, Any], done: bool) -> list[str]:
    """Readable progress / phase-done parts; consumed keys are removed from fields."""
    parts = [str(fields.pop("phase", ""))]
    files, files_total = fields.pop("files", None), fields.pop("files_total", None)
    nbytes, bytes_total = fields.pop("bytes", None), fields.pop("bytes_total", None)
    percent = fields.pop("percent", None)
    rate = fields.pop("rate_mib_s", None)
    elapsed, eta = fields.pop("elapsed_s", None), fields.pop("eta_s", None)
    if percent is not None:
        parts.append(f"{percent:5.1f} %")
    if files is not None:
        parts.append(f"{files}/{files_total} files" if files_total else f"{files} files")
    if nbytes is not None:
        parts.append(f"{size(nbytes)}/{size(bytes_total)}" if bytes_total else size(nbytes))
    if rate is not None:
        parts.append(f"{rate} MiB/s")
    if elapsed is not None:
        parts.append(f"in {duration(elapsed)}" if done else f"elapsed {duration(elapsed)}")
    if eta is not None:
        parts.append(f"left {duration(eta)}")
    return parts


class TextFormatter(logging.Formatter):
    """One readable line per event; colours for warnings and errors on a terminal."""

    COLOURS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[2m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
    }
    RESET = "\033[0m"

    def __init__(self, colour: bool = False) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        fields = _ordered(_fields(record))
        event = record.getMessage()
        parts = [
            datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            f"{record.levelname:<7}",
            f"{event:<{EVENT_WIDTH}}",
        ]
        context = [str(fields.pop(key)) for key in CONTEXT if fields.get(key) is not None]
        for key in CONTEXT:
            fields.pop(key, None)
        details: list[str] = []
        if event in ("progress", "phase done"):
            details = _progress(fields, done=event == "phase done")
        details += [f"{key}={_value(value)}" for key, value in fields.items()]
        line = " ".join(parts) + "  ".join(["", *context, *details]).rstrip()
        colour = self.COLOURS.get(record.levelname) if self.colour else None
        return f"{colour}{line}{self.RESET}" if colour else line


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def resolve_format(fmt: str, stream: TextIO | None = None) -> str:
    """auto -> text on a terminal, json otherwise."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown log format: {fmt!r}")
    if fmt != "auto":
        return fmt
    return "text" if _is_terminal(stream or sys.stderr) else "json"


def configure_logging(level: str, fmt: str = "json") -> None:
    stream = sys.stderr
    handler = logging.StreamHandler(stream)
    if resolve_format(fmt, stream) == "text":
        colour = _is_terminal(stream) and "NO_COLOR" not in os.environ
        colour = colour and os.environ.get("TERM") != "dumb"
        handler.setFormatter(TextFormatter(colour))
    else:
        handler.setFormatter(JsonFormatter())
    LOGGER.handlers[:] = [handler]
    LOGGER.setLevel(level.upper())
    LOGGER.propagate = False


def log_event(level: int, event: str, **fields: object) -> None:
    LOGGER.log(level, event, extra={"fields": fields})
