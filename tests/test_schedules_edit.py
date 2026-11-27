"""Editing a schedule on its own page instead of deleting and re-creating it."""

import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import AuditLog, Repository, Schedule
from mirrorhub.services import scheduler


def _setup() -> tuple[int, int, int]:
    with Session(get_engine()) as db:
        first = Repository(slug="first", name="First", kind="generic", tool="wget")
        second = Repository(slug="second", name="Second", kind="generic", tool="rsync")
        db.add(first)
        db.add(second)
        db.commit()
        schedule = Schedule(
            repo_id=first.id or 0,
            job_type="sync",
            cron_expr="0 3 * * *",
            preset_label="daily",
            enabled=True,
            chain_export=True,
            next_fire_at=scheduler.next_after("0 3 * * *"),
        )
        db.add(schedule)
        db.commit()
        return first.id or 0, second.id or 0, schedule.id or 0


def _selected(page: str, name: str) -> str:
    block = re.search(rf'<select name="{name}".*?</select>', page, re.S)
    assert block, name
    option = re.search(r'<option value="([^"]*)" selected>', block.group(0))
    assert option, name
    return option.group(1)


def test_list_links_to_the_edit_page(admin: TestClient) -> None:
    _, _, schedule_id = _setup()
    assert f'href="/schedules/{schedule_id}/edit"' in admin.get("/schedules").text


def test_edit_page_shows_the_current_values(admin: TestClient) -> None:
    first, _, schedule_id = _setup()
    page = admin.get(f"/schedules/{schedule_id}/edit").text
    assert f'action="/schedules/{schedule_id}"' in page
    assert _selected(page, "repo_id") == str(first)
    assert _selected(page, "job_type") == "sync"
    assert _selected(page, "preset_label") == "daily"
    assert _selected(page, "chain_export") == "always"
    assert 'name="cron_expr" value="0 3 * * *"' in page
    assert 'name="enabled" checked' in page


def test_edit_page_unknown_schedule(admin: TestClient) -> None:
    assert admin.get("/schedules/999/edit").status_code == 404


def test_update_changes_every_field(admin: TestClient, csrf_token: str) -> None:
    _, second, schedule_id = _setup()
    response = admin.post(
        f"/schedules/{schedule_id}",
        data={
            "csrf_token": csrf_token,
            "repo_id": str(second),
            "job_type": "rehash",
            "preset_label": "monthly",
            "cron_expr": "0 3 1 * *",
            "chain_export": "always",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(get_engine()) as db:
        schedule = db.get(Schedule, schedule_id)
        assert schedule is not None
        assert schedule.repo_id == second
        assert schedule.job_type == "rehash"
        assert schedule.cron_expr == "0 3 1 * *"
        assert schedule.preset_label == "monthly"
        assert schedule.enabled is False
        assert schedule.chain_export is None  # only sync schedules chain an export
        assert schedule.next_fire_at is not None
        assert schedule.next_fire_at.day == 1
        entry = db.exec(select(AuditLog).where(AuditLog.action == "schedule.update")).one()
        changes = entry.detail["changes"]
    assert changes["job_type"] == ["sync", "rehash"]
    assert changes["enabled"] == [True, False]
    assert changes["chain_export"] == ["always", "inherit"]


def test_update_error_keeps_the_input(admin: TestClient, csrf_token: str) -> None:
    first, _, schedule_id = _setup()
    page = admin.post(
        f"/schedules/{schedule_id}",
        data={
            "csrf_token": csrf_token,
            "repo_id": str(first),
            "job_type": "export",
            "cron_expr": "not a cron",
            "enabled": "on",
        },
    ).text
    assert f"Edit schedule {schedule_id}" in page
    assert 'name="cron_expr" value="not a cron"' in page
    assert _selected(page, "job_type") == "export"
    with Session(get_engine()) as db:
        schedule = db.get(Schedule, schedule_id)
        assert schedule is not None
        assert schedule.cron_expr == "0 3 * * *" and schedule.job_type == "sync"


def test_create_error_keeps_the_input(admin: TestClient, csrf_token: str) -> None:
    _setup()
    page = admin.post(
        "/schedules",
        data={"csrf_token": csrf_token, "repo_id": "0", "cron_expr": "5 4 * * *"},
    ).text
    assert "select a repository" in page
    assert 'name="cron_expr" value="5 4 * * *"' in page
