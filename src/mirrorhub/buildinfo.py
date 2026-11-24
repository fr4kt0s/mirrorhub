"""Which commit this code was built from (standard library only).

Sources, first match wins:

1. ``MIRRORHUB_COMMIT`` in the environment (override, e.g. for builds without git).
2. ``mirrorhub/_build.py``, written into the receiver importer archive by
   scripts/build_importer.py.
3. The ``.git`` directory next to ``src/`` (a source checkout, or the minimal copy of
   ``.git/HEAD`` and the branch refs that the Docker image carries in ``/app/.git``).

The importer compares its own commit with the sender commit recorded in each export
and warns when they differ.
"""

from __future__ import annotations

import importlib
import os
import re
from functools import cache
from pathlib import Path

SHORT = 12
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")


def _git_dir() -> Path:
    # src/mirrorhub/buildinfo.py -> repository (or /app in the image) -> .git
    return Path(__file__).resolve().parents[2] / ".git"


def resolve_head(git_dir: Path) -> str | None:
    """Full commit id of HEAD from the files alone (no git binary needed)."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if _SHA_RE.fullmatch(head):
        return head  # detached HEAD, e.g. a CI checkout
    if not head.startswith("ref: "):
        return None
    ref = head.removeprefix("ref: ").strip()
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        return None
    try:
        value = (git_dir / ref).read_text(encoding="ascii").strip()
        if _SHA_RE.fullmatch(value):
            return value
    except (OSError, UnicodeDecodeError):
        pass
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in packed.splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA_RE.fullmatch(sha):
            return sha
    return None


@cache
def commit() -> str | None:
    """Short commit id (12 hex digits, possibly with a suffix such as -dirty), or None."""
    override = os.environ.get("MIRRORHUB_COMMIT", "").strip()
    if override:
        return override[:40]
    try:
        written = importlib.import_module("mirrorhub._build")  # only in the archive
    except ImportError:
        pass
    else:
        value = getattr(written, "COMMIT", None)
        if isinstance(value, str) and value:
            return value
    full = resolve_head(_git_dir())
    return full[:SHORT] if full else None


def describe() -> str:
    """Version string for --version and the UI: '0.1.0 (9d24408606f0)'."""
    from mirrorhub import __version__

    return f"{__version__} ({commit() or 'unknown commit'})"
