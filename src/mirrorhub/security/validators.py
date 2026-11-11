"""Strict validation for repository configuration and schedules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from croniter import croniter

MAX_LIST_ITEMS = 64
MAX_EXTRA_FILES = 32
MAX_STRING = 200

SLUG_RE = re.compile(
    r"(?=.{1,253}$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ROOT_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
ARCH_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,15}$")
PATTERN_RE = re.compile(r"^[!+-]?[A-Za-z0-9*?\[\]._+/:~-]{1,200}$")
URL_RE = re.compile(
    r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?"
    r"(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$"
)
REL_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
ARG_RE = re.compile(r"^--[a-z0-9][a-z0-9-]{0,40}(?:=[A-Za-z0-9._:/-]{1,64})?$")

COMMON_ARCHES = (
    "amd64",
    "arm64",
    "armhf",
    "i386",
    "ppc64el",
    "riscv64",
    "s390x",
    "x86_64",
    "aarch64",
    "none",
)


@dataclass(frozen=True)
class ArgSpec:
    kind: str
    lo: int = 0
    hi: int = 0
    values: tuple[str, ...] = ()


ALLOWED_ARGS: dict[str, dict[str, ArgSpec]] = {
    "debmirror": {
        "--progress": ArgSpec("flag"),
        "--verbose": ArgSpec("flag"),
        "--debug": ArgSpec("flag"),
    },
    "rsync": {
        "--compress": ArgSpec("flag"),
        "--partial": ArgSpec("flag"),
        "--stats": ArgSpec("flag"),
        "--human-readable": ArgSpec("flag"),
        "--itemize-changes": ArgSpec("flag"),
        "--checksum": ArgSpec("flag"),
        "--no-motd": ArgSpec("flag"),
    },
    "wget": {
        "--continue": ArgSpec("flag"),
        "--no-verbose": ArgSpec("flag"),
        "--tries": ArgSpec("int", lo=1, hi=20),
        "--timeout": ArgSpec("int", lo=1, hi=300),
    },
    "ftpsync": {},
}


def validate_slug(value: str) -> str | None:
    if not SLUG_RE.fullmatch(value):
        return (
            "repository ID: 1-253 characters; lowercase letters, digits, hyphens and dots; "
            "each dot-separated part must start and end with a letter or digit"
        )
    return None


def validate_name(value: str) -> str | None:
    if not value or len(value) > 128:
        return "name: required, at most 128 characters"
    if any(ord(character) < 32 for character in value):
        return "name: control characters are not allowed"
    return None


def validate_host(value: str) -> str | None:
    if not value or len(value) > 253:
        return "upstream host: required, at most 253 characters"
    if "://" in value or "/" in value or any(character.isspace() for character in value):
        return "upstream host: hostname only, no scheme and no path"
    labels = value.rstrip(".").split(".")
    if any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        return "upstream host: invalid hostname label"
    return None


def validate_root(value: str) -> str | None:
    if not value:
        return "upstream root: required (use / for the mirror root)"
    if value != "/" and value.endswith("/"):
        return "upstream root: no trailing slash (use /debian, not /debian/)"
    if ".." in value or "//" in value:
        return "upstream root: '..' and '//' are not allowed"
    if not ROOT_RE.fullmatch(value):
        return "upstream root: must be an absolute path such as /debian"
    return None


def validate_list_field(
    name: str,
    values: list[str],
    *,
    max_items: int = MAX_LIST_ITEMS,
) -> str | None:
    if len(values) > max_items:
        return f"{name}: at most {max_items} entries"
    for item in values:
        if not TOKEN_RE.fullmatch(item):
            return f"{name}: invalid entry {item[:40]!r}"
    return None


def validate_architectures(values: list[str]) -> str | None:
    if not values:
        return (
            "architectures: at least one entry is required (use 'none' to mirror no architectures)"
        )
    if len(values) > 16:
        return "architectures: at most 16 entries"
    for item in values:
        if item != "none" and not ARCH_RE.fullmatch(item):
            return f"architectures: invalid entry {item[:20]!r}"
    if "none" in values and len(values) > 1:
        return "architectures: 'none' cannot be combined with other entries"
    return None


def validate_patterns(name: str, values: list[str]) -> str | None:
    if len(values) > MAX_LIST_ITEMS:
        return f"{name}: at most {MAX_LIST_ITEMS} patterns"
    for item in values:
        if not PATTERN_RE.fullmatch(item):
            return f"{name}: invalid pattern {item[:40]!r}"
    return None


def validate_cron(expr: str) -> str | None:
    text = expr.strip()
    if len(text.split()) != 5:
        return "cron: expected 5 fields (minute hour day month weekday)"
    if not croniter.is_valid(text):
        return "cron: invalid expression"
    return None


def validate_url(value: str) -> str | None:
    if len(value) > 500 or not URL_RE.fullmatch(value):
        return f"url: invalid or unsupported URL {value[:80]!r}"
    return None


def validate_rel_path(value: str) -> str | None:
    if not value or len(value) > 240:
        return "path: required, at most 240 characters"
    if value.startswith("/") or ".." in value.split("/"):
        return f"path: must be relative and must not contain '..' ({value[:60]!r})"
    if not REL_PATH_RE.fullmatch(value):
        return f"path: invalid characters in {value[:60]!r}"
    return None


def validate_extra_args(tool: str, args: list[str]) -> str | None:
    specs = ALLOWED_ARGS.get(tool, {})
    if len(args) > 16:
        return "extra arguments: at most 16 entries"

    for raw in args:
        if not ARG_RE.fullmatch(raw):
            return f"extra arguments: malformed entry {raw[:40]!r}"

        name, _, value = raw.partition("=")
        spec = specs.get(name)
        if spec is None:
            return f"extra arguments: '{name}' is not allowed for {tool}"
        if spec.kind == "flag" and value:
            return f"extra arguments: '{name}' takes no value"
        if spec.kind != "flag" and not value:
            return f"extra arguments: '{name}' requires a value"
        if (
            spec.kind == "int"
            and value
            and (not value.isdigit() or not spec.lo <= int(value) <= spec.hi)
        ):
            return f"extra arguments: '{name}' must be an integer {spec.lo}-{spec.hi}"
        if spec.kind == "enum" and value not in spec.values:
            return f"extra arguments: '{name}' must be one of {', '.join(spec.values)}"

    return None


def validate_bwlimit(value: int) -> str | None:
    if not isinstance(value, int) or not 0 <= value <= 10_000_000:
        return "bandwidth limit: 0 (unlimited) to 10000000 KB/s"
    return None


def validate_retry_policy(policy: dict[str, Any]) -> str | None:
    attempts = policy.get("max_attempts")
    if not isinstance(attempts, int) or not 1 <= attempts <= 10:
        return "retry: max attempts must be 1-10"
    if policy.get("backoff") not in ("fixed", "linear", "exponential"):
        return "retry: backoff must be fixed, linear or exponential"

    for key, lo, hi in (("base_seconds", 5, 86400), ("max_seconds", 5, 604800)):
        value = policy.get(key)
        if not isinstance(value, int) or not lo <= value <= hi:
            return f"retry: {key} must be {lo}-{hi}"

    codes = policy.get("retryable_exit_codes")
    if not isinstance(codes, list) or not codes:
        return "retry: at least one retryable exit code"
    if len(codes) > 32:
        return "retry: at most 32 exit codes"
    for code in codes:
        if not isinstance(code, int) or not 0 <= code <= 255:
            return "retry: exit codes must be integers 0-255"
    return None


def validate_export_fields(mode: str, fmt: str, split_mb: int) -> str | None:
    if mode not in ("incr", "full"):
        return "export mode: incr or full"
    if fmt not in ("tar", "tar.zst"):
        return "export format: tar or tar.zst"
    if not isinstance(split_mb, int) or not 0 <= split_mb <= 102400:
        return "export split: 0 (no split) up to 102400 MB"
    return None


def validate_extra_files(items: list[dict[str, str]]) -> str | None:
    if len(items) > MAX_EXTRA_FILES:
        return "extra files: at most {MAX_EXTRA_FILES} entries"
    for item in items:
        error = validate_url(item.get("url", "")) or validate_rel_path(item.get("path", ""))
        if error:
            return f"extra files: {error}"
    return None
