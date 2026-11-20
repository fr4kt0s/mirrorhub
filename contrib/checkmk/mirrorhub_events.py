#!/usr/bin/env python3
"""Checkmk local check for MirrorHub event files received through the data diode.

MirrorHub's file notification channel writes one JSON file per event to
`<drop folder>/_events/`. After the pickup tool has transferred them, this script
reads them on the receiving side and prints Checkmk local-check lines:

    MirrorHub sync <repo>     last sync result and age of the last successful sync
    MirrorHub export <repo>   last export result and age of the last published export
    MirrorHub sender disk     low-disk alerts from the sender
    MirrorHub event feed      whether event files arrive at all

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
      "ignore_repos": [],
      "repos": {
        "ubuntu": {"sync_warn_hours": 30, "sync_crit_hours": 60},
        "archlinux": {"export": false}
      }
    }

"feed_warn_hours" > 0 turns the event feed WARN when no event arrived for that
long. "export": false hides the export service for a repository.

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
    "ignore_repos": [],
    "repos": {},
}
CONFIG_PATH = "/etc/check_mk/mirrorhub_events.json"
STATE_VERSION = 1


@dataclass
class Result:
    status: int
    service: str
    text: str
    metrics: list[str] = field(default_factory=list)

    def line(self) -> str:
        metrics = "|".join(self.metrics) if self.metrics else "-"
        text = " ".join(self.text.split())  # one line, no control characters
        return f'{self.status} "{self.service}" {metrics} {text}'


def load_config(path: str | None = None) -> dict[str, Any]:
    config = dict(DEFAULTS)
    candidate = Path(path or os.environ.get("MIRRORHUB_EVENTS_CONFIG") or CONFIG_PATH)
    if candidate.is_file():
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{candidate}: expected a JSON object")
        config.update(loaded)
    return config


def parse_time(value: Any) -> datetime | None:
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
    return parse_time(event.get("finished_at")) or parse_time(event.get("time"))


def _newer(current: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
    if current is None:
        return True
    return str(candidate.get("time", "")) > str(current.get("time", ""))


def _keep(event: dict[str, Any], when: datetime, *keys: str) -> dict[str, Any]:
    record = {key: event.get(key) for key in keys if key in event}
    record["time"] = when.astimezone(timezone.utc).isoformat()
    return record


def merge_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Fold one event into the per-repository state, keeping the newest of each kind."""
    kind = event.get("event")
    when = event_time(event)
    if when is None or event.get("dry_run") is True:
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
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": STATE_VERSION}
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
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


def run(config: dict[str, Any], now: datetime | None = None) -> list[Result]:
    now = now or datetime.now(timezone.utc)
    state_path = Path(str(config["state_file"]))
    state = load_state(state_path)
    events, files, errors = read_events(Path(str(config["events_dir"])))
    for event in events:
        merge_event(state, event)
    save_state(state_path, state)

    ignored = set(config.get("ignore_repos") or [])
    results: list[Result] = [feed_result(state, files, errors, config, now)]
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
    return results


def main() -> int:
    try:
        results = run(load_config())
    except Exception as exc:  # must always print a valid service line
        results = [Result(UNKNOWN, "MirrorHub event feed", f"check failed: {exc}")]
    for result in results:
        print(result.line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
