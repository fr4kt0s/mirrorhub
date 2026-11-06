"""Audit log viewer with filters (HTMX partial for live filtering)."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from mirrorhub.services import audit
from mirrorhub.web.deps import CurrentUser, get_db, render, require_user

router = APIRouter()
PAGE_SIZE = 50


def _parse_day(value: str) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    except ValueError:
        return None


@router.get("/audit")
def audit_page(
    request: Request,
    actor: str = "",
    action: str = "",
    text: str = "",
    since: str = "",
    until: str = "",
    page: int = 0,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    page = max(page, 0)
    until_dt = _parse_day(until)
    rows = audit.query(
        db,
        actor=actor.strip()[:64],
        action=action.strip()[:64],
        text=text.strip()[:128],
        since=_parse_day(since),
        until=(until_dt + timedelta(days=1)) if until_dt else None,
        limit=PAGE_SIZE + 1,
        offset=page * PAGE_SIZE,
    )
    context = {
        "rows": rows[:PAGE_SIZE],
        "has_next": len(rows) > PAGE_SIZE,
        "page": page,
        "f": {"actor": actor, "action": action, "text": text, "since": since, "until": until},
    }
    template = "partials/audit_results.html" if request.headers.get("hx-request") else "audit.html"
    return render(request, template, context)
