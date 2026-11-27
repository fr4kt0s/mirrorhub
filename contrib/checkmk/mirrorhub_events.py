#!/usr/bin/env python3
"""Checkmk local check for MirrorHub event files on the receiving side.

Two producers write one JSON file per event:

- the sender's file notification channel, `<drop folder>/_events/`, transferred
  through the data diode by the pickup tool ("events_dir");
- the receiver's importer, `events_dir` in importer.toml ("importer_events_dir",
  optional).

This script reads both and prints Checkmk local-check lines:

    MirrorHub sync <repo>     last sync result and age of the last successful sync
    MirrorHub export <repo>   last export result and age of the last published export
    MirrorHub sender disk     low-disk alerts from the sender
    MirrorHub event feed      whether sender event files arrive at all
    MirrorHub import <repo>   last import result; published exports not imported yet
    MirrorHub tree <repo>     receiver tree vs. files.json (importer tree checks)
    MirrorHub importer        importer event files and the last import run

Step-by-step setup: docs/checkmk.md in the MirrorHub repository.

It keeps the newest result per repository in a small state file, so the services
keep their state after the event files have been deleted.

Install on the receiving host (Python 3.9 or newer, standard library only):

    install -m 0755 mirrorhub_events.py /usr/lib/check_mk_agent/local/mirrorhub_events

Configuration (optional): /etc/check_mk/mirrorhub_events.json, or the path in
MIRRORHUB_EVENTS_CONFIG. All keys are optional:

    {
      "events_dir": "/srv/diode-inbox/_events",
      "state_file": "/var/lib/check_mk_agent/mirrorhub_events_state.json",
      "sync_warn_hours": 26,
      "sync_crit_hours": 50,
      "export_warn_hours": 26,
      "export_crit_hours": 50,
      "disk_alert_hours": 24,
      "feed_warn_hours": 0,
      "importer_events_dir": "",
      "import_lag_warn_hours": 6,
      "import_lag_crit_hours": 24,
      "importer_run_warn_hours": 0,
      "tree_drift_state": 1,
      "tree_check_warn_hours": 0,
      "tree_hash_warn_hours": 0,
      "ignore_repos": [],
      "repos": {
        "ubuntu": {"sync_warn_hours": 30, "sync_crit_hours": 60},
        "archlinux": {"export": false, "import": false}
      }
    }

"feed_warn_hours" > 0 turns the event feed WARN when no event arrived for that
long. "export": false hides the export service for a repository, "import": false
the import service, "tree": false the tree service. The importer services need
"importer_events_dir"; hour limits set to 0 are off.

Run it by hand to see the output:

    /usr/lib/check_mk_agent/local/mirrorhub_events
"""

# timezone.utc instead of datetime.UTC: the receiving host may run Python 3.9/3.10.
# ruff: noqa: UP017

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OK, WARN, CRIT, UNKNOWN = 0, 1, 2, 3

DEFAULTS: dict[str, Any] = {
    "events_dir": "/srv/diode-inbox/_events",
    "state_file": "/var/lib/check_mk_agent/mirrorhub_events_state.json",
    "sync_warn_hours": 26,
    "sync_crit_hours": 50,
    "export_warn_hours": 26,
    "export_crit_hours": 50,
    "disk_alert_hours": 24,
    "feed_warn_hours": 0,
    "importer_events_dir": "",
    "import_lag_warn_hours": 6,
    "import_lag_crit_hours": 24,
    "importer_run_warn_hours": 0,
    "tree_drift_state": WARN,
    "tree_check_warn_hours": 0,
    "tree_hash_warn_hours": 0,
    "ignore_repos": [],
    "repos": {},
}
CONFIG_PATH = "/etc/check_mk/mirrorhub_events.json"
STATE_VERSION = 1


@dataclass
class Result:
    """One Checkmk local-check service: state, name, metrics and text."""

    status: int
    service: str
    text: str
    metrics: list[str] = field(default_factory=list)

    def line(self) -> str:
        """Return the service as one local-check output line."""
        metrics = "|".join(self.metrics) if self.metrics else "-"
        text = " ".join(self.text.split())  # one line, no control characters
        return f'{self.status} "{self.service}" {metrics} {text}'


def load_config(path: str | None = None) -> dict[str, Any]:
    """Read the JSON configuration over the defaults (missing file: defaults only)."""
    config = dict(DEFAULTS)
    candidate = Path(path or os.environ.get("MIRRORHUB_EVENTS_CONFIG") or CONFIG_PATH)
    if candidate.is_file():
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{candidate}: expected a JSON object")
        config.update(loaded)
    return config


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO 8601 time from an event; None if missing or invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def event_time(event: dict[str, Any]) -> datetime | None:
    """Return when an event happened (finished_at, else time)."""
    return parse_time(event.get("finished_at")) or parse_time(event.get("time"))


def _newer(current: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
    if current is None:
        return True
    return str(candidate.get("time", "")) > str(current.get("time", ""))


def _keep(event: dict[str, Any], when: datetime, *keys: str) -> dict[str, Any]:
    record = {key: event.get(key) for key in keys if key in event}
    record["time"] = when.astimezone(timezone.utc).isoformat()
    return record


IMPORTER_SLOTS = {
    "import_applied": (
        "import_ok",
        ("export_id", "export_mode", "payload_count", "deleted_count", "duration_s"),
    ),
    "import_adopted": ("import_ok", ("export_id", "export_mode")),
    "import_failed": ("import_failed", ("export_id", "error", "error_kind", "exit_code")),
    "tree_check": (
        "tree",
        ("export_id", "origin", "hashed", "differences", "counts", "entries"),
    ),
    "check_failed": ("check_failed", ("error", "error_kind", "exit_code")),
}


def _same_or_newer(current: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
    """Return True if candidate is at least as new as current.

    Importer files are read in name order, which is chronological: on equal times
    the later file wins.
    """
    return current is None or str(candidate.get("time", "")) >= str(current.get("time", ""))


def merge_importer_event(state: dict[str, Any], event: dict[str, Any], when: datetime) -> None:
    """Merge an importer event: newest import, failure and tree check per repository, last run."""
    importer = state.setdefault("importer", {})
    stamp = when.astimezone(timezone.utc).isoformat()
    if stamp > str(importer.get("newest", "")):
        importer["newest"] = stamp
    kind = event.get("event")
    if kind == "import_run":
        record = _keep(event, when, "repo", "scope", "exit_code", "duration_s")
        if _same_or_newer(importer.get("run"), record):
            importer["run"] = record
        return
    repo = event.get("repo")
    if kind not in IMPORTER_SLOTS or not isinstance(repo, str) or not repo:
        return
    slot, keys = IMPORTER_SLOTS[str(kind)]
    record = _keep(event, when, *keys)
    if kind == "import_adopted":
        record["adopted"] = True
    entry = state.setdefault("repos", {}).setdefault(repo, {})
    if _same_or_newer(entry.get(slot), record):
        entry[slot] = record
    if (
        kind == "tree_check"
        and event.get("hashed") is True
        and _same_or_newer(entry.get("tree_hashed"), record)
    ):
        entry["tree_hashed"] = record


def merge_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Fold one event into the per-repository state, keeping the newest of each kind."""
    kind = event.get("event")
    when = event_time(event)
    if when is None or event.get("dry_run") is True:
        return
    if event.get("source") == "importer":
        merge_importer_event(state, event, when)
        return

    feed = state.setdefault("feed", {})
    stamp = when.astimezone(timezone.utc).isoformat()
    if stamp > str(feed.get("newest", "")):
        feed["newest"] = stamp

    if kind == "low_disk":
        record = _keep(event, when, "level", "percent", "path")
        if _newer(state.get("disk"), record):
            state["disk"] = record
        return

    repo = event.get("repo")
    if not isinstance(repo, str) or not repo:
        return
    entry = state.setdefault("repos", {}).setdefault(repo, {})
    job_type = event.get("job_type")

    slot: str | None = None
    record: dict[str, Any] | None = None
    if kind == "job_succeeded" and job_type == "sync":
        slot, record = "sync_ok", _keep(event, when, "job_id")
    elif kind == "job_failed" and job_type == "sync":
        slot = "sync_failed"
        record = _keep(event, when, "job_id", "exit_code", "error_summary", "attempt")
    elif kind == "export_published":
        slot = "export_ok"
        record = _keep(
            event,
            when,
            "job_id",
            "export_id",
            "export_mode",
            "file_count",
            "deleted_count",
            "archive_bytes",
        )
    elif kind == "job_failed" and job_type == "export":
        slot = "export_failed"
        record = _keep(event, when, "job_id", "exit_code", "error_summary")

    if slot is not None and record is not None and _newer(entry.get(slot), record):
        entry[slot] = record


def read_events(directory: Path) -> tuple[list[dict[str, Any]], int, int]:
    """Return parsed events, the number of files and the number of unreadable files."""
    events: list[dict[str, Any]] = []
    files = errors = 0
    if not directory.is_dir():
        return events, files, errors
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        files += 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors += 1
            continue
        if isinstance(document, dict):
            events.append(document)
        else:
            errors += 1
    return events, files, errors


def load_state(path: Path) -> dict[str, Any]:
    """Read the state file; a fresh state if it is missing, unreadable or outdated."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": STATE_VERSION}
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write the state file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def _age_s(record: dict[str, Any] | None, now: datetime) -> float | None:
    when = parse_time((record or {}).get("time"))
    return None if when is None else max((now - when).total_seconds(), 0.0)


def _human(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _option(config: dict[str, Any], repo: str, key: str) -> Any:
    overrides = config.get("repos", {}).get(repo, {})
    return overrides.get(key, config.get(key))


def _age_status(age: float, warn_s: float, crit_s: float) -> int:
    if age >= crit_s:
        return CRIT
    if age >= warn_s:
        return WARN
    return OK


def sync_result(repo: str, entry: dict[str, Any], config: dict[str, Any], now: datetime) -> Result:
    """Return the service of a repository's syncs.

    CRIT after a failure without a later success; WARN/CRIT when the last success is old.
    """
    warn_s = float(_option(config, repo, "sync_warn_hours")) * 3600
    crit_s = float(_option(config, repo, "sync_crit_hours")) * 3600
    ok, failed = entry.get("sync_ok"), entry.get("sync_failed")
    ok_age, failed_age = _age_s(ok, now), _age_s(failed, now)
    metrics = (
        [f"last_success_age={int(ok_age)};{int(warn_s)};{int(crit_s)}"]
        if ok_age is not None
        else []
    )
    service = f"MirrorHub sync {repo}"

    if failed is not None and (ok_age is None or (failed_age or 0) < ok_age):
        summary = str(failed.get("error_summary") or "").strip()
        text = (
            f"Last sync FAILED {_human(failed_age or 0)} ago (job {failed.get('job_id')}, "
            f"exit code {failed.get('exit_code')})" + (f": {summary}" if summary else "")
        )
        if ok_age is not None:
            text += f"; last success {_human(ok_age)} ago"
        return Result(CRIT, service, text, metrics)

    if ok_age is None:
        return Result(UNKNOWN, service, "No sync result received yet")
    status = _age_status(ok_age, warn_s, crit_s)
    return Result(
        status, service, f"Last sync OK {_human(ok_age)} ago (job {ok.get('job_id')})", metrics
    )


def export_result(
    repo: str, entry: dict[str, Any], config: dict[str, Any], now: datetime
) -> Result:
    """Return the service of a repository's exports.

    CRIT after a failure without a later success; WARN/CRIT when the last export is old.
    """
    warn_s = float(_option(config, repo, "export_warn_hours")) * 3600
    crit_s = float(_option(config, repo, "export_crit_hours")) * 3600
    ok, failed = entry.get("export_ok"), entry.get("export_failed")
    ok_age, failed_age = _age_s(ok, now), _age_s(failed, now)
    service = f"MirrorHub export {repo}"
    metrics: list[str] = []
    if ok is not None and ok_age is not None:
        metrics.append(f"last_export_age={int(ok_age)};{int(warn_s)};{int(crit_s)}")
        if isinstance(ok.get("archive_bytes"), int):
            metrics.append(f"archive_bytes={ok['archive_bytes']}")
        if isinstance(ok.get("file_count"), int):
            metrics.append(f"files={ok['file_count']}")

    if failed is not None and (ok_age is None or (failed_age or 0) < ok_age):
        summary = str(failed.get("error_summary") or "").strip()
        text = f"Last export FAILED {_human(failed_age or 0)} ago (job {failed.get('job_id')})" + (
            f": {summary}" if summary else ""
        )
        return Result(CRIT, service, text, metrics)

    if ok is None or ok_age is None:
        return Result(UNKNOWN, service, "No published export received yet")
    size = ok.get("archive_bytes")
    size_text = f", {size / 1e9:.2f} GB" if isinstance(size, int) else ""
    text = (
        f"Last export {ok.get('export_id')} ({ok.get('export_mode')}, "
        f"{ok.get('file_count')} files{size_text}) {_human(ok_age)} ago"
    )
    return Result(_age_status(ok_age, warn_s, crit_s), service, text, metrics)


def disk_result(state: dict[str, Any], config: dict[str, Any], now: datetime) -> Result | None:
    """Return the service for the sender's low_disk events; None if there was none."""
    disk = state.get("disk")
    if disk is None:
        return None
    age = _age_s(disk, now)
    window_s = float(config["disk_alert_hours"]) * 3600
    percent = disk.get("percent")
    metrics = [f"used_percent={percent}"] if isinstance(percent, (int, float)) else []
    if age is None or age > window_s:
        return Result(
            OK,
            "MirrorHub sender disk",
            f"No disk alert in the last {config['disk_alert_hours']} h "
            f"(last: {disk.get('level')} {_human(age or 0)} ago)",
        )
    status = CRIT if disk.get("level") == "critical" else WARN
    return Result(
        status,
        "MirrorHub sender disk",
        f"Sender disk {disk.get('level')}: {percent}% used on {disk.get('path')}, "
        f"{_human(age)} ago",
        metrics,
    )


def feed_result(
    state: dict[str, Any], files: int, errors: int, config: dict[str, Any], now: datetime
) -> Result:
    """Return the service of the event feed: file count, read errors, newest event age."""
    newest = parse_time(state.get("feed", {}).get("newest"))
    parts = [f"{files} event files in {config['events_dir']}"]
    status = OK
    if errors:
        status = WARN
        parts.append(f"{errors} unreadable")
    if newest is None:
        parts.append("no events received yet")
    else:
        age = max((now - newest).total_seconds(), 0.0)
        parts.append(f"newest event {_human(age)} ago")
        limit = float(config.get("feed_warn_hours") or 0) * 3600
        if limit and age > limit:
            status = WARN
    if not Path(str(config["events_dir"])).is_dir():
        status = UNKNOWN
        parts.insert(0, "events directory missing")
    return Result(status, "MirrorHub event feed", ", ".join(parts))


def _metric(name: str, value: float, warn: float = 0, crit: float = 0) -> str:
    """name=value with warn/crit levels only where they are set (> 0)."""
    levels = [str(int(level)) if level else "" for level in (warn, crit)]
    while levels and not levels[-1]:
        levels.pop()
    return ";".join([f"{name}={int(value)}", *levels])


def _ago(record: dict[str, Any] | None, now: datetime) -> str:
    age = _age_s(record, now)
    return "never" if age is None else f"{_human(age)} ago"


def import_result(
    repo: str, entry: dict[str, Any], config: dict[str, Any], now: datetime
) -> Result:
    """Return the service of a repository's imports.

    CRIT on a failure without a later success; WARN/CRIT when the sender published
    an export that has not been imported for import_lag_*_hours.
    """
    service = f"MirrorHub import {repo}"
    ok, failed, published = (
        entry.get("import_ok"),
        entry.get("import_failed"),
        entry.get("export_ok"),
    )
    ok_age, failed_age = _age_s(ok, now), _age_s(failed, now)
    applied = str((ok or {}).get("export_id") or "")
    metrics: list[str] = []
    if ok is not None and ok_age is not None:
        metrics.append(f"last_import_age={int(ok_age)}")
        if isinstance(ok.get("duration_s"), (int, float)):
            metrics.append(f"import_duration={ok['duration_s']}")
        if isinstance(ok.get("payload_count"), int):
            metrics.append(f"import_files={ok['payload_count']}")

    lag_s = 0.0
    waiting = published is not None and str(published.get("export_id") or "") > applied
    if waiting:
        lag_s = _age_s(published, now) or 0.0
    warn_s = float(_option(config, repo, "import_lag_warn_hours")) * 3600
    crit_s = float(_option(config, repo, "import_lag_crit_hours")) * 3600
    metrics.append(_metric("import_lag", lag_s, warn_s, crit_s))
    last = (
        f"last import {ok.get('export_id')} {_human(ok_age)} ago"
        if ok is not None and ok_age is not None
        else "no import yet"
    )

    if failed is not None and (ok_age is None or (failed_age or 0) < ok_age):
        export = failed.get("export_id") or "(before any export)"
        text = (
            f"Import of {export} FAILED {_human(failed_age or 0)} ago "
            f"({failed.get('error_kind')}, exit code {failed.get('exit_code')}): "
            f"{failed.get('error')}; {last}"
        )
        return Result(CRIT, service, text, metrics)

    if waiting and published is not None:
        status = OK
        if crit_s and lag_s >= crit_s:
            status = CRIT
        elif warn_s and lag_s >= warn_s:
            status = WARN
        text = (
            f"Export {published.get('export_id')} published {_human(lag_s)} ago, "
            f"not imported yet; {last}"
        )
        return Result(status, service, text, metrics)

    if ok is None or ok_age is None:
        return Result(UNKNOWN, service, "No import result received yet", metrics)
    how = "adopted" if ok.get("adopted") else f"{ok.get('export_mode')}"
    details = [how]
    if isinstance(ok.get("payload_count"), int):
        details.append(f"{ok['payload_count']} files")
    if isinstance(ok.get("duration_s"), (int, float)):
        details.append(f"{ok['duration_s']} s")
    text = f"Last import {ok.get('export_id')} ({', '.join(details)}) {_human(ok_age)} ago"
    return Result(OK, service, text, metrics)


def tree_result(repo: str, entry: dict[str, Any], config: dict[str, Any], now: datetime) -> Result:
    """Receiver tree vs. files.json, from the newest importer tree check."""
    service = f"MirrorHub tree {repo}"
    last, hashed = entry.get("tree"), entry.get("tree_hashed")
    failed = entry.get("check_failed")
    differences = int((last or {}).get("differences") or 0)
    metrics = [f"tree_differences={differences}"]
    if last is None:
        if failed is not None:
            text = f"Check FAILED {_ago(failed, now)}: {failed.get('error')}"
            return Result(WARN, service, text, metrics)
        return Result(UNKNOWN, service, "No tree check received yet", metrics)
    kind = "SHA-256" if last.get("hashed") else "quick"
    checked = f"{kind} check ({last.get('origin')}) {_ago(last, now)}"
    hash_note = f"last SHA-256 check {_ago(hashed, now)}"

    if failed is not None and (_age_s(failed, now) or 0) < (_age_s(last, now) or 0):
        return Result(
            WARN,
            service,
            f"Last check FAILED {_ago(failed, now)}: {failed.get('error')}; previous: {checked}",
            metrics,
        )
    if differences:
        counts = last.get("counts") or {}
        listed = ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
        text = (
            f"Tree differs from {last.get('export_id')} in {differences} places "
            f"({listed}), {checked}; run 'check --output-dir' for the lists"
        )
        return Result(int(_option(config, repo, "tree_drift_state")), service, text, metrics)

    status = OK
    notes: list[str] = []
    check_limit = float(_option(config, repo, "tree_check_warn_hours")) * 3600
    hash_limit = float(_option(config, repo, "tree_hash_warn_hours")) * 3600
    if check_limit and (_age_s(last, now) or 0) > check_limit:
        status = WARN
        notes.append(
            f"no tree check for more than {_option(config, repo, 'tree_check_warn_hours')} h"
        )
    hashed_age = _age_s(hashed, now)
    if hash_limit and (hashed_age is None or hashed_age > hash_limit):
        status = WARN
        notes.append(
            f"no SHA-256 check for more than {_option(config, repo, 'tree_hash_warn_hours')} h"
        )
    entries = last.get("entries")
    size = f" ({entries} entries)" if isinstance(entries, int) else ""
    text = f"Tree matches {last.get('export_id')}{size}, {checked}, {hash_note}"
    if notes:
        text += "; " + "; ".join(notes)
    return Result(status, service, text, metrics)


def importer_result(
    state: dict[str, Any], files: int, errors: int, config: dict[str, Any], now: datetime
) -> Result:
    """Return the service of the importer's event files and its last run (heartbeat)."""
    directory = str(config["importer_events_dir"])
    importer = state.get("importer", {})
    run = importer.get("run")
    parts = [f"{files} event files in {directory}"]
    metrics: list[str] = []
    status = OK
    if errors:
        status = WARN
        parts.append(f"{errors} unreadable")
    run_age = _age_s(run, now)
    limit = float(config.get("importer_run_warn_hours") or 0) * 3600
    if run is None or run_age is None:
        parts.append("no import run recorded yet")
    else:
        metrics.append(_metric("last_run_age", run_age, limit))
        code = run.get("exit_code")
        parts.append(
            f"last import run ({run.get('scope')}) {_human(run_age)} ago, exit code {code}"
        )
        if code not in (0, 11):
            status = max(status, WARN)
        if limit and run_age > limit:
            status = max(status, WARN)
            parts.append(f"no import run for more than {config['importer_run_warn_hours']} h")
    if not Path(directory).is_dir():
        status = UNKNOWN
        parts.insert(0, "importer events directory missing")
    return Result(status, "MirrorHub importer", ", ".join(parts), metrics)


def run(config: dict[str, Any], now: datetime | None = None) -> list[Result]:
    """Read new event files, update the state and return all services."""
    now = now or datetime.now(timezone.utc)
    state_path = Path(str(config["state_file"]))
    state = load_state(state_path)
    events, files, errors = read_events(Path(str(config["events_dir"])))
    importer_dir = str(config.get("importer_events_dir") or "")
    importer_events: list[dict[str, Any]] = []
    importer_files = importer_errors = 0
    if importer_dir:
        importer_events, importer_files, importer_errors = read_events(Path(importer_dir))
    for event in events:
        if event.get("source") != "importer":  # importer files belong in their own directory
            merge_event(state, event)
    for event in importer_events:
        if event.get("source") == "importer":
            merge_event(state, event)
    save_state(state_path, state)

    ignored = set(config.get("ignore_repos") or [])
    results: list[Result] = [feed_result(state, files, errors, config, now)]
    if importer_dir:
        results.append(importer_result(state, importer_files, importer_errors, config, now))
    disk = disk_result(state, config, now)
    if disk is not None:
        results.append(disk)
    for repo, entry in sorted(state.get("repos", {}).items()):
        if repo in ignored:
            continue
        if "sync_ok" in entry or "sync_failed" in entry:
            results.append(sync_result(repo, entry, config, now))
        if _option(config, repo, "export") is not False and (
            "export_ok" in entry or "export_failed" in entry
        ):
            results.append(export_result(repo, entry, config, now))
        if not importer_dir:
            continue
        if _option(config, repo, "import") is not False and (
            "import_ok" in entry or "import_failed" in entry or "export_ok" in entry
        ):
            results.append(import_result(repo, entry, config, now))
        if _option(config, repo, "tree") is not False and (
            "tree" in entry or "check_failed" in entry
        ):
            results.append(tree_result(repo, entry, config, now))
    return results


def main() -> int:
    """Print the local-check lines; always exit 0 (Checkmk reads the states from the lines)."""
    try:
        results = run(load_config())
    except Exception as exc:  # must always print a valid service line
        results = [Result(UNKNOWN, "MirrorHub event feed", f"check failed: {exc}")]
    for result in results:
        print(result.line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
