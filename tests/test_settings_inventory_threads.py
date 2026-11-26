"""MIRRORHUB_INVENTORY_THREADS and its old name MIRRORHUB_EXPORT_WORKERS (#64)."""

import logging

import pytest
from pydantic import ValidationError

from mirrorhub.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIRRORHUB_INVENTORY_THREADS", raising=False)
    monkeypatch.delenv("MIRRORHUB_EXPORT_WORKERS", raising=False)


def test_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert Settings().inventory_threads == 8
    assert "deprecated" not in caplog.text


def test_new_name(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MIRRORHUB_INVENTORY_THREADS", "12")
    with caplog.at_level(logging.WARNING):
        assert Settings().inventory_threads == 12
    assert "deprecated" not in caplog.text


def test_old_name_still_works_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MIRRORHUB_EXPORT_WORKERS", "4")
    with caplog.at_level(logging.WARNING):
        assert Settings().inventory_threads == 4
    assert "MIRRORHUB_EXPORT_WORKERS is deprecated (used for now)" in caplog.text
    assert "MIRRORHUB_INVENTORY_THREADS" in caplog.text


def test_new_name_wins(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MIRRORHUB_INVENTORY_THREADS", "16")
    monkeypatch.setenv("MIRRORHUB_EXPORT_WORKERS", "2")
    with caplog.at_level(logging.WARNING):
        assert Settings().inventory_threads == 16
    assert "ignored, MIRRORHUB_INVENTORY_THREADS is set" in caplog.text


def test_keyword_and_range() -> None:
    assert Settings(inventory_threads=3).inventory_threads == 3
    with pytest.raises(ValidationError):
        Settings(inventory_threads=0)
    with pytest.raises(ValidationError):
        Settings(inventory_threads=65)
