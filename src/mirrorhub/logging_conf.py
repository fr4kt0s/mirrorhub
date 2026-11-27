"""Structured logging: plain text or one JSON object per line. Common secrets are redacted."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(\S+)")
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def redact(text: str) -> str:
    """Replace the value after ``password=``, ``token:`` and similar keys with ``***``."""
    return _SECRET_RE.sub(r"\1\2***", text)


class RedactingFilter(logging.Filter):
    """Redact secrets in every log message before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Format the message once, redact it and keep the record."""
        record.msg = redact(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON object, including the fields passed in ``extra``."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single-line JSON object."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Send all logging to stderr as text or JSON (fmt "json"), with secrets redacted."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
