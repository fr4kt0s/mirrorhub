"""contrib/checkmk/mirrorhub_events.py: Checkmk local check for event files."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Job, Repository
from mirrorhub.services import notify
from mirrorhub.settings import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "checkmk" / "mirrorhub_events.py"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mirrorhub_events", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    events = tmp_path / "_events"
    events.mkdir()
    config = dict(check.DEFAULTS)
    config.update(events_dir=str(events), state_file=str(tmp_path / "state" / "state.json"))
    return events, config


def _write(events: Path, name: str, **fields: Any) -> None:
    (events / f"{name}.json").write_text(json.dumps(fields), encoding="utf-8")


def _sync(events: Path, name: str, event: str, hours_ago: float, **extra: Any) -> None:
    _write(
        events,
        name,
        event=event,
        repo="ubuntu",
        job_type="sync",
        job_id=extra.pop("job_id", 1),
        finished_at=_iso(NOW - timedelta(hours=hours_ago)),
        **extra,
    )


def _lines(config: dict[str, Any]) -> dict[str, str]:
    results = check.run(config, NOW)
    return {result.service: result.line() for result in results}


def test_recent_successful_sync_is_ok(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _sync(events, "a", "job_succeeded", 2, job_id=7)

    line = _lines(config)["MirrorHub sync ubuntu"]
    assert line.startswith('0 "MirrorHub sync ubuntu" last_success_age=7200;93600;180000 ')
    assert "Last sync OK 2.0 h ago (job 7)" in line


def test_failure_after_success_is_critical(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _sync(events, "a", "job_succeeded", 10, job_id=7)
    _sync(events, "b", "job_failed", 1, job_id=8, exit_code=23, error_summary="rsync broke")

    line = _lines(config)["MirrorHub sync ubuntu"]
    assert line.startswith('2 "MirrorHub sync ubuntu"')
    assert "job 8, exit code 23): rsync broke" in line
    assert "last success 10.0 h ago" in line


def test_success_after_failure_recovers(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _sync(events, "a", "job_failed", 5, job_id=8, exit_code=23)
    _sync(events, "b", "job_succeeded", 1, job_id=9)
    assert _lines(config)["MirrorHub sync ubuntu"].startswith("0 ")


@pytest.mark.parametrize(("hours", "status"), [(27, "1"), (51, "2")])
def test_stale_success_warns_then_crits(
    setup: tuple[Path, dict[str, Any]], hours: int, status: str
) -> None:
    events, config = setup
    _sync(events, "a", "job_succeeded", hours)
    assert _lines(config)["MirrorHub sync ubuntu"].startswith(f"{status} ")


def test_per_repository_thresholds(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    config["repos"] = {"ubuntu": {"sync_warn_hours": 100, "sync_crit_hours": 200}}
    _sync(events, "a", "job_succeeded", 60)
    assert _lines(config)["MirrorHub sync ubuntu"].startswith("0 ")


def test_export_service(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _write(
        events,
        "e",
        event="export_published",
        repo="ubuntu",
        job_type="export",
        export_id="20261120T100000Z-incr",
        export_mode="incr",
        file_count=12,
        archive_bytes=2_500_000_000,
        finished_at=_iso(NOW - timedelta(hours=2)),
    )
    line = _lines(config)["MirrorHub export ubuntu"]
    assert line.startswith('0 "MirrorHub export ubuntu" last_export_age=7200;93600;180000|')
    assert "archive_bytes=2500000000|files=12" in line
    assert "20261120T100000Z-incr (incr, 12 files, 2.50 GB) 2.0 h ago" in line

    _write(
        events,
        "f",
        event="job_failed",
        repo="ubuntu",
        job_type="export",
        job_id=99,
        error_summary="disk full",
        finished_at=_iso(NOW - timedelta(minutes=10)),
    )
    line = _lines(config)["MirrorHub export ubuntu"]
    assert line.startswith("2 ") and "disk full" in line


def test_export_service_can_be_disabled(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    config["repos"] = {"ubuntu": {"export": False}}
    _write(
        events,
        "e",
        event="export_published",
        repo="ubuntu",
        finished_at=_iso(NOW - timedelta(hours=1)),
    )
    assert "MirrorHub export ubuntu" not in _lines(config)


def test_state_survives_deleted_event_files(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _sync(events, "a", "job_failed", 1, exit_code=1)
    assert _lines(config)["MirrorHub sync ubuntu"].startswith("2 ")
    for path in events.iterdir():
        path.unlink()
    assert _lines(config)["MirrorHub sync ubuntu"].startswith("2 ")


def test_dry_runs_and_test_events_are_ignored(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _sync(events, "a", "job_failed", 1, dry_run=True)
    _write(events, "t", event="test", channel="drop", time=_iso(NOW))
    assert "MirrorHub sync ubuntu" not in _lines(config)


def test_disk_alert_window(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    _write(
        events,
        "d",
        event="low_disk",
        level="critical",
        percent=96.5,
        path="/data",
        time=_iso(NOW - timedelta(hours=3)),
    )
    assert _lines(config)["MirrorHub sender disk"].startswith(
        '2 "MirrorHub sender disk" used_percent=96.5 '
    )
    later = dict(config)
    results = {r.service: r for r in check.run(later, NOW + timedelta(hours=30))}
    assert results["MirrorHub sender disk"].status == check.OK


def test_feed_reports_unreadable_files_and_missing_directory(
    setup: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    events, config = setup
    (events / "broken.json").write_text("{not json", encoding="utf-8")
    (events / ".hidden.json.tmp").write_text("{}", encoding="utf-8")
    line = _lines(config)["MirrorHub event feed"]
    assert line.startswith("1 ") and "1 event files" in line and "1 unreadable" in line

    config["events_dir"] = str(tmp_path / "missing")
    assert _lines(config)["MirrorHub event feed"].startswith("3 ")


def test_feed_warns_when_events_stop(setup: tuple[Path, dict[str, Any]]) -> None:
    events, config = setup
    config["feed_warn_hours"] = 12
    _sync(events, "a", "job_succeeded", 20)
    assert _lines(config)["MirrorHub event feed"].startswith("1 ")


def test_main_never_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setenv("MIRRORHUB_EVENTS_CONFIG", str(bad))
    assert check.main() == 0
    assert capsys.readouterr().out.startswith('3 "MirrorHub event feed" - check failed')


def test_reads_files_written_by_the_file_channel(configured: Settings, tmp_path: Path) -> None:
    """Contract between producer (notify file channel) and consumer (this check)."""
    with Session(get_engine()) as db:
        repo = Repository(slug="ubuntu", name="ubuntu")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        channel = notify.create_channel(
            db, name="drop", channel_type="file", config={"log_mode": "none"}, actor="test"
        )
        for event in ("job_succeeded", "job_failed"):
            notify.create_rule(
                db, channel_id=channel.id or 0, event=event, repo_id=None, actor="test"
            )
        finished = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        db.add(
            Job(
                repo_id=repo.id or 0,
                type="sync",
                status=JobStatus.FAILED.value,
                exit_code=23,
                error_summary="rsync broke",
                finished_at=finished,
            )
        )
        db.commit()
        notify.sweep_finished(db)
    assert all(result.ok for result in notify.deliver_due())

    config = dict(check.DEFAULTS)
    config.update(
        events_dir=str(notify.events_dir(configured)),
        state_file=str(tmp_path / "checkmk-state.json"),
    )
    lines = {result.service: result.line() for result in check.run(config)}
    assert lines["MirrorHub sync ubuntu"].startswith('2 "MirrorHub sync ubuntu"')
    assert "exit code 23): rsync broke" in lines["MirrorHub sync ubuntu"]
    assert lines["MirrorHub event feed"].startswith("0 ")
