"""contrib/checkmk/mirrorhub_events.py: services from importer event files (docs/checkmk.md)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mirrorhub.importer import events as events_module
from mirrorhub.importer.cli import main
from test_importer import ID1, ID2, T1, T2, build_export

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "checkmk" / "mirrorhub_events.py"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
EXPORT_A = "20261123T080000Z-incr"
EXPORT_B = "20261124T020000Z-incr"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mirrorhub_events_importer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    sender, importer = tmp_path / "_events", tmp_path / "importer-events"
    sender.mkdir()
    importer.mkdir()
    config = dict(check.DEFAULTS)
    config.update(
        events_dir=str(sender),
        importer_events_dir=str(importer),
        state_file=str(tmp_path / "state.json"),
    )
    return sender, importer, config


def _write(directory: Path, name: str, hours_ago: float, **fields: Any) -> None:
    document = {"time": _iso(NOW - timedelta(hours=hours_ago)), **fields}
    (directory / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")


def _importer(directory: Path, name: str, hours_ago: float, event: str, **fields: Any) -> None:
    _write(directory, name, hours_ago, event=event, source="importer", repo="ubuntu", **fields)


def _published(directory: Path, name: str, hours_ago: float, export_id: str) -> None:
    _write(
        directory,
        name,
        hours_ago,
        event="export_published",
        repo="ubuntu",
        job_type="export",
        export_id=export_id,
        export_mode="incr",
        file_count=3,
    )


def _lines(config: dict[str, Any], now: datetime = NOW) -> dict[str, str]:
    return {result.service: result.line() for result in check.run(config, now)}


def test_importer_services_only_with_importer_events_dir(
    dirs: tuple[Path, Path, dict[str, Any]],
) -> None:
    sender, importer, config = dirs
    _published(sender, "p", 3, EXPORT_A)
    _importer(importer, "a", 2, "import_applied", export_id=EXPORT_A)
    config["importer_events_dir"] = ""
    services = _lines(config)
    assert "MirrorHub import ubuntu" not in services and "MirrorHub importer" not in services


def test_last_import_ok(dirs: tuple[Path, Path, dict[str, Any]]) -> None:
    sender, importer, config = dirs
    _published(sender, "p", 3, EXPORT_A)
    _importer(
        importer,
        "a",
        2,
        "import_applied",
        export_id=EXPORT_A,
        export_mode="incr",
        payload_count=37,
        duration_s=12.5,
    )
    line = _lines(config)["MirrorHub import ubuntu"]
    assert line.startswith(
        '0 "MirrorHub import ubuntu" last_import_age=7200|import_duration=12.5'
        "|import_files=37|import_lag=0;21600;86400 "
    )
    assert line.endswith(f"Last import {EXPORT_A} (incr, 37 files, 12.5 s) 2.0 h ago")


@pytest.mark.parametrize(("hours", "status"), [(2, "0"), (7, "1"), (25, "2")])
def test_published_export_not_imported_yet(
    dirs: tuple[Path, Path, dict[str, Any]], hours: float, status: str
) -> None:
    sender, importer, config = dirs
    _importer(importer, "a", 30, "import_applied", export_id=EXPORT_A, export_mode="incr")
    _published(sender, "p", hours, EXPORT_B)
    line = _lines(config)["MirrorHub import ubuntu"]
    assert line.startswith(f'{status} "MirrorHub import ubuntu"')
    assert f"Export {EXPORT_B} published {hours:.1f} h ago, not imported yet" in line
    assert f"last import {EXPORT_A} 30.0 h ago" in line


def test_superseded_export_is_not_pending(dirs: tuple[Path, Path, dict[str, Any]]) -> None:
    sender, importer, config = dirs
    _published(sender, "p1", 30, EXPORT_A)
    _published(sender, "p2", 20, EXPORT_B)
    _importer(importer, "a", 10, "import_superseded", export_id=EXPORT_A)
    _importer(importer, "b", 10, "import_applied", export_id=EXPORT_B, export_mode="incr")
    assert _lines(config)["MirrorHub import ubuntu"].startswith("0 ")


def test_failed_import_is_critical_until_the_next_success(
    dirs: tuple[Path, Path, dict[str, Any]],
) -> None:
    _, importer, config = dirs
    _importer(importer, "a", 30, "import_applied", export_id=EXPORT_A, export_mode="incr")
    _importer(
        importer,
        "b",
        1,
        "import_failed",
        export_id=EXPORT_B,
        error="checksum mismatch for archive part data.tar",
        error_kind="VerificationFailed",
        exit_code=3,
    )
    line = _lines(config)["MirrorHub import ubuntu"]
    assert line.startswith('2 "MirrorHub import ubuntu"')
    assert f"Import of {EXPORT_B} FAILED 1.0 h ago (VerificationFailed, exit code 3)" in line
    assert f"last import {EXPORT_A} 30.0 h ago" in line

    _importer(importer, "c", 0.5, "import_applied", export_id=EXPORT_B, export_mode="incr")
    assert _lines(config)["MirrorHub import ubuntu"].startswith("0 ")


def test_tree_differences_and_check_ages(dirs: tuple[Path, Path, dict[str, Any]]) -> None:
    _, importer, config = dirs
    _importer(
        importer,
        "t1",
        48,
        "tree_check",
        export_id=EXPORT_A,
        origin="check",
        hashed=True,
        differences=0,
        entries=421,
    )
    _importer(
        importer,
        "t2",
        1,
        "tree_check",
        export_id=EXPORT_B,
        origin="import",
        hashed=False,
        differences=0,
        entries=421,
    )
    line = _lines(config)["MirrorHub tree ubuntu"]
    assert line == (
        '0 "MirrorHub tree ubuntu" tree_differences=0 Tree matches '
        f"{EXPORT_B} (421 entries), quick check (import) 1.0 h ago, "
        "last SHA-256 check 2.0 days ago"
    )

    config["tree_hash_warn_hours"] = 24
    line = _lines(config)["MirrorHub tree ubuntu"]
    assert line.startswith("1 ") and "no SHA-256 check for more than 24 h" in line

    _importer(
        importer,
        "t3",
        0.5,
        "tree_check",
        export_id=EXPORT_B,
        origin="check",
        hashed=False,
        differences=3,
        counts={"missing": 2, "extra": 1},
    )
    line = _lines(config)["MirrorHub tree ubuntu"]
    assert line.startswith('1 "MirrorHub tree ubuntu" tree_differences=3 ')
    assert f"Tree differs from {EXPORT_B} in 3 places (extra 1, missing 2)" in line
    config["tree_drift_state"] = 2
    assert _lines(config)["MirrorHub tree ubuntu"].startswith("2 ")


def test_failed_check_and_disabled_services(dirs: tuple[Path, Path, dict[str, Any]]) -> None:
    _, importer, config = dirs
    _importer(importer, "f", 1, "check_failed", error="no export applied", exit_code=5)
    assert "Check FAILED 1.0 h ago" in _lines(config)["MirrorHub tree ubuntu"]
    config["repos"] = {"ubuntu": {"tree": False, "import": False}}
    _importer(importer, "a", 1, "import_applied", export_id=EXPORT_A)
    services = _lines(config)
    assert "MirrorHub tree ubuntu" not in services and "MirrorHub import ubuntu" not in services


def test_importer_service_heartbeat(dirs: tuple[Path, Path, dict[str, Any]]) -> None:
    _, importer, config = dirs
    line = _lines(config)["MirrorHub importer"]
    assert line.startswith('0 "MirrorHub importer" - 0 event files') and "no import run" in line

    _write(importer, "r", 3, event="import_run", source="importer", scope="all", exit_code=0)
    line = _lines(config)["MirrorHub importer"]
    assert line.startswith('0 "MirrorHub importer" last_run_age=10800 ')
    assert "last import run (all) 3.0 h ago, exit code 0" in line

    config["importer_run_warn_hours"] = 2
    assert _lines(config)["MirrorHub importer"].startswith(
        '1 "MirrorHub importer" last_run_age=10800;7200 '
    )

    config["importer_run_warn_hours"] = 0
    _write(importer, "s", 1, event="import_run", source="importer", scope="all", exit_code=7)
    assert _lines(config)["MirrorHub importer"].startswith("1 ")

    config["importer_events_dir"] = str(importer.parent / "missing")
    assert _lines(config)["MirrorHub importer"].startswith("3 ")


def test_files_in_the_wrong_directory_are_ignored(
    dirs: tuple[Path, Path, dict[str, Any]],
) -> None:
    sender, importer, config = dirs
    _importer(sender, "a", 1, "import_applied", export_id=EXPORT_A)
    _published(importer, "p", 1, EXPORT_A)
    services = _lines(config)
    assert "MirrorHub import ubuntu" not in services and "MirrorHub export ubuntu" not in services


@pytest.fixture
def importer_setup(tmp_path: Path) -> Any:
    settings = tmp_path / "importer.toml"
    settings.write_text(
        'export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\nevents_dir = "ev"\n'
    )
    yield tmp_path / "in", ["--config", str(settings)], tmp_path / "ev"
    events_module.configure(None)


def test_reads_files_written_by_the_importer(importer_setup: Any, tmp_path: Path) -> None:
    """Contract between producer (importer events_dir) and consumer (this check)."""
    inbox, base, directory = importer_setup
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    (tmp_path / "out" / "ubuntu" / "pool/a.deb").unlink()
    assert main([*base, "check", "--slug", "ubuntu"]) == 11

    sender = tmp_path / "_events"
    sender.mkdir()
    config = dict(check.DEFAULTS)
    config.update(
        events_dir=str(sender),
        importer_events_dir=str(directory),
        state_file=str(tmp_path / "checkmk-state.json"),
    )
    now = datetime.now(UTC)
    lines = {result.service: result.line() for result in check.run(config, now)}
    assert lines["MirrorHub import ubuntu"].startswith('0 "MirrorHub import ubuntu"')
    assert f"Last import {ID2} (incr, 3 files, " in lines["MirrorHub import ubuntu"]
    assert lines["MirrorHub tree ubuntu"].startswith('1 "MirrorHub tree ubuntu" tree_differences=1')
    assert "(missing 1)" in lines["MirrorHub tree ubuntu"]
    assert lines["MirrorHub importer"].startswith('0 "MirrorHub importer"')
    assert "exit code 0" in lines["MirrorHub importer"]
