"""Pure parts of scripts/smoke_test.py (no Docker needed)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"
REQUIRED = ("web", "worker", "worker-export", "proxy")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


smoke = _load()


def _ps(*rows: tuple[str, str, str]) -> list[dict[str, object]]:
    return [{"Service": name, "State": state, "Health": health} for name, state, health in rows]


def _healthy_stack(workers: int = 1) -> list[dict[str, object]]:
    rows = [("web", "running", "healthy"), ("worker-export", "running", "healthy")]
    rows += [("worker", "running", "healthy")] * workers
    rows += [("proxy", "running", "healthy"), ("init-volumes", "exited", "")]
    return _ps(*rows)


def test_healthy_stack_passes() -> None:
    status, detail = smoke.evaluate_services(_healthy_stack(), REQUIRED)
    assert status == smoke.PASS
    assert "web=healthy" in detail
    assert "init-volumes=exited" in detail


def test_replicas_are_summarised() -> None:
    status, detail = smoke.evaluate_services(_healthy_stack(workers=3), REQUIRED)
    assert status == smoke.PASS
    assert "worker=3xhealthy" in detail
    assert smoke.replica_count(_healthy_stack(workers=3), "worker") == 3


def test_unhealthy_replica_fails() -> None:
    services = [
        *_healthy_stack(workers=2),
        {"Service": "worker", "State": "running", "Health": "unhealthy"},
    ]
    with pytest.raises(smoke.CheckError, match="unhealthy: worker="):
        smoke.evaluate_services(services, REQUIRED)


def test_starting_is_a_warning() -> None:
    services = _healthy_stack()
    services[0]["Health"] = "starting"
    status, detail = smoke.evaluate_services(services, REQUIRED)
    assert status == smoke.WARN
    assert "web=starting" in detail


def test_stopped_or_missing_service_fails() -> None:
    services = _healthy_stack()
    services[1]["State"] = "exited"
    with pytest.raises(smoke.CheckError, match="not running: worker-export"):
        smoke.evaluate_services(services, REQUIRED)

    without_proxy = [row for row in _healthy_stack() if row["Service"] != "proxy"]
    with pytest.raises(smoke.CheckError, match="missing services: proxy"):
        smoke.evaluate_services(without_proxy, REQUIRED)


def test_services_without_healthcheck_only_need_to_run() -> None:
    services = _ps(*((name, "running", "") for name in REQUIRED))
    status, _detail = smoke.evaluate_services(services, REQUIRED)
    assert status == smoke.PASS
