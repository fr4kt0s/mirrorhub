"""Dashboard card "Next schedules"."""

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.models import Repository, Schedule, utcnow


def _schedule(db: Session, slug: str, name: str, *, job_type: str, enabled: bool) -> None:
    repo = Repository(slug=slug, name=name)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    db.add(
        Schedule(
            repo_id=repo.id or 0,
            job_type=job_type,
            cron_expr="17 3 * * *",
            enabled=enabled,
            next_fire_at=utcnow() + timedelta(hours=1),
        )
    )
    db.commit()


def _next_schedules_card(html: str) -> str:
    match = re.search(r"<h2>Next schedules</h2>(.*?)</article>", html, re.S)
    assert match, "card 'Next schedules' missing"
    return match.group(1)


def test_next_schedules_show_the_repository(admin: TestClient) -> None:
    with Session(get_engine()) as db:
        _schedule(db, "dash-debian", "Debian main", job_type="export", enabled=True)
        _schedule(db, "dash-paused", "Paused repo", job_type="sync", enabled=False)

    card = _next_schedules_card(admin.get("/").text)

    assert '<strong title="Debian main">dash-debian</strong>' in card
    assert '<span class="muted">export</span>' in card
    assert "<code>17 3 * * *</code>" in card
    assert "dash-paused" not in card  # disabled schedules are not listed


def test_next_schedules_empty(admin: TestClient) -> None:
    assert "Nothing scheduled." in _next_schedules_card(admin.get("/").text)
