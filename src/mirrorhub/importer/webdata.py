"""Read-only data for the web UI: event history, difference reports, export details.

Everything here only reads files the importer writes (event files in events_dir,
reports in the reports directory, the receiver state) or the sender wrote (export
metadata in the inbox). Nothing is verified here; the importer verifies an export
when it applies it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mirrorhub.importer import repair
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config
from mirrorhub.importer.state import RepoState
from mirrorhub.importer.tree import REPORT_FILES

# keys of summary.txt (file names without .txt)
REPORT_KINDS = tuple(name.removesuffix(".txt") for name in REPORT_FILES.values())

HISTORY_KINDS = (
    "import_run",
    "import_applied",
    "import_adopted",
    "import_failed",
    "import_superseded",
    "tree_check",
    "check_failed",
)


def reports_dir(config: Config) -> Path:
    """Where difference lists are written and read: reports_dir, else <state_dir>/reports."""
    return config.reports_dir or config.state_dir / "reports"


def parse_time(value: object) -> datetime | None:
    """Parse an ISO 8601 time from an event file; None if missing or invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- Event history --------------------------------------------------------------------


class EventIndex:
    """Importer event files of events_dir, parsed once per file (cache by name and mtime)."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def events(self) -> list[dict[str, Any]]:
        """All importer events, oldest first (file names sort chronologically)."""
        if self.directory is None or not self.directory.is_dir():
            return []
        seen: dict[str, tuple[float, dict[str, Any] | None]] = {}
        result: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(path.name)
            if cached is None or cached[0] != mtime:
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    document = None
                if not isinstance(document, dict) or document.get("source") != "importer":
                    document = None
                cached = (mtime, document)
            seen[path.name] = cached
            if cached[1] is not None and cached[1].get("event") in HISTORY_KINDS:
                result.append(cached[1])
        self._cache = seen
        return result

    def history(self, slug: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        """Newest first, optionally for one repository (runs of all repositories included)."""
        found = [
            event
            for event in self.events()
            if slug is None
            or event.get("repo") == slug
            or (event.get("event") == "import_run" and not event.get("repo"))
        ]
        return list(reversed(found))[:limit]

    def repo_status(self) -> dict[str, RepoStatus]:
        """Return the latest import, tree check and failure events per repository."""
        status: dict[str, RepoStatus] = {}
        for event in self.events():
            repo = event.get("repo")
            if not isinstance(repo, str) or not repo:
                continue
            entry = status.setdefault(repo, RepoStatus())
            kind = event.get("event")
            if kind in ("import_applied", "import_adopted"):
                entry.import_ok = event
            elif kind == "import_failed":
                entry.import_failed = event
            elif kind == "tree_check":
                entry.tree = event
                if event.get("hashed") is True:
                    entry.tree_hashed = event
            elif kind == "check_failed":
                entry.check_failed = event
        return status


@dataclass
class RepoStatus:
    """Newest importer events of one repository."""

    import_ok: dict[str, Any] | None = None
    import_failed: dict[str, Any] | None = None
    tree: dict[str, Any] | None = None
    tree_hashed: dict[str, Any] | None = None
    check_failed: dict[str, Any] | None = None

    def failed_last(self) -> bool:
        """Return True if the newest import attempt failed (no success after it)."""
        if self.import_failed is None:
            return False
        if self.import_ok is None:
            return True
        return str(self.import_failed.get("time")) > str(self.import_ok.get("time"))


# ---- Difference reports ---------------------------------------------------------------


@dataclass
class Report:
    """A difference report of one repository: summary.txt and the per-kind lists."""

    slug: str
    summary: dict[str, str]
    kinds: dict[str, list[str]] = field(default_factory=dict)
    truncated: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total number of differences from summary.txt (0 if unreadable)."""
        try:
            return int(self.summary.get("total", "0"))
        except ValueError:
            return 0


def read_report(config: Config, slug: str, limit: int = 1000) -> Report | None:
    """DIR/<slug>/summary.txt and the per-kind lists (first `limit` lines of each)."""
    if not SLUG_RE.fullmatch(slug):
        return None
    directory = reports_dir(config) / slug
    try:
        text = (directory / "summary.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    summary = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    report = Report(slug, summary)
    for kind, name in REPORT_FILES.items():
        try:
            lines = (directory / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        report.kinds[kind] = lines[:limit]
        if len(lines) > limit:
            report.truncated[kind] = len(lines)
    return report


def report_file(config: Config, slug: str, kind: str) -> Path | None:
    """Path of one list for download, only for known slugs and kinds."""
    name = REPORT_FILES.get(kind)
    if name is None or not SLUG_RE.fullmatch(slug):
        return None
    path = reports_dir(config) / slug / name
    return path if path.is_file() else None


def repair_request(config: Config, slug: str) -> tuple[Path, dict[str, Any]] | None:
    """Return the repair request next to the report (#43): path and document, if any."""
    if not SLUG_RE.fullmatch(slug):
        return None
    path = repair.request_path(reports_dir(config), slug)
    document = repair.read_request(path) if path.is_file() else None
    return None if document is None else (path, document)


def repair_file(config: Config, slug: str, name: str) -> Path | None:
    """Return the request or its .sha256 file for download, by exact name only."""
    if not SLUG_RE.fullmatch(slug):
        return None
    request = repair.request_name(slug)
    if name not in (request, f"{request}.sha256"):
        return None
    path = reports_dir(config) / slug / name
    return path if path.is_file() else None


# ---- Export details -------------------------------------------------------------------


@dataclass
class ExportInfo:
    """What the web UI shows about an export in the inbox."""

    slug: str
    export_id: str
    directory: Path
    published: bool
    manifest: dict[str, Any]
    archive_bytes: int
    parts: int
    signed: bool
    status: str  # applied, superseded, pending
    chain: str  # human-readable fit with the receiver state
    needs_rebase: bool
    importable: bool
    error: str = ""

    @property
    def mode(self) -> str:
        """Export mode (full or incr), from manifest.json or the export id."""
        return str(self.manifest.get("mode") or self.export_id.rsplit("-", 1)[-1])

    @property
    def counts(self) -> dict[str, Any]:
        """The manifest's file counts (empty if missing)."""
        counts = self.manifest.get("counts")
        return counts if isinstance(counts, dict) else {}

    @property
    def sender_commit(self) -> str:
        """MirrorHub commit of the sender that built the export (empty if unknown)."""
        versions = self.manifest.get("tool_versions")
        value = versions.get("mirrorhub_commit") if isinstance(versions, dict) else None
        return str(value or "")


def export_info(config: Config, slug: str, export_id: str, state: RepoState) -> ExportInfo | None:
    """Describe an export in the inbox for the web UI; None if the id or directory is invalid."""
    if not SLUG_RE.fullmatch(slug) or not EXPORT_ID_RE.fullmatch(export_id):
        return None
    directory = config.export_dir / slug / export_id
    if not directory.is_dir() or directory.is_symlink():
        return None
    manifest: dict[str, Any] = {}
    error = ""
    try:
        loaded = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded
        else:
            error = "manifest.json is not an object"
    except (OSError, ValueError) as exc:
        error = f"manifest.json unreadable: {exc}"
    names = manifest.get("archive_parts")
    parts = [str(name) for name in names] if isinstance(names, list) else []
    size = 0
    for name in parts:
        try:
            size += (directory / name).stat().st_size
        except OSError:
            error = error or f"archive part missing: {name}"
    published = (directory / ".done").is_file()

    if state.applied(export_id):
        status = "applied"
    elif state.superseded(export_id) or state.is_superseded_by_state(export_id):
        status = "superseded"
    else:
        status = "pending"
    mode = str(manifest.get("mode") or export_id.rsplit("-", 1)[-1])
    base = manifest.get("base_export_id")
    last = state.last_export_id
    needs_rebase = False
    if status == "applied":
        chain = "applied" + (" (the receiver's current state)" if export_id == last else "")
    elif status == "superseded":
        chain = "skipped: a later export already covers it"
    elif mode == "full":
        if last is None:
            chain = "first import into this repository (an existing tree needs a rebase)"
        else:
            chain = f"full export over an existing state (last applied {last}): needs a rebase"
            needs_rebase = True
    elif last is None:
        chain = "incremental, but nothing has been applied yet: import a full export first"
    elif base == last:
        chain = f"fits: built on {base}, the last applied export"
    else:
        chain = f"does not fit: built on {base}, last applied is {last}"
    importable = (
        status == "pending" and published and not error and not chain.startswith(("does", "incr"))
    )
    return ExportInfo(
        slug,
        export_id,
        directory,
        published,
        manifest,
        size,
        len(parts),
        (directory / "MANIFEST.sha256.sig").is_file(),
        status,
        chain,
        needs_rebase,
        importable,
        error,
    )
