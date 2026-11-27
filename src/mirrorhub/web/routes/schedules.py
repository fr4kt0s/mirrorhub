"""Schedule CRUD routes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, select
from starlette.datastructures import FormData

from mirrorhub import cronhelp
from mirrorhub.enums import SCHEDULE_JOB_TYPES
from mirrorhub.models import Repository, Schedule, utcnow
from mirrorhub.security import validators
from mirrorhub.services import audit, repos, scheduler
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()

CHAIN_CHOICES: dict[str, bool | None] = {"inherit": None, "always": True, "never": False}


def _chain_choice(value: object) -> bool | str | None:
    """Map the form value to a tri-state; return an error string for unknown values."""
    text = str(value or "inherit")
    if text not in CHAIN_CHOICES:
        return "export after sync must be inherit, always or never"
    return CHAIN_CHOICES[text]


NEXT_RUNS = 3
CRON_CHOICES: dict[str, Any] = {
    "frequencies": cronhelp.FREQUENCIES,
    "weekdays": cronhelp.WEEKDAYS,
    "intervals": cronhelp.INTERVALS,
}

DEFAULT_VALUES: dict[str, Any] = {
    "repo_id": None,
    "job_type": "sync",
    "cron_expr": "0 3 * * *",
    "enabled": True,
    "chain_export": "inherit",
}


def _chain_name(value: bool | None) -> str:
    return next(name for name, choice in CHAIN_CHOICES.items() if choice is value)


def _values_of(schedule: Schedule) -> dict[str, Any]:
    return {
        "repo_id": schedule.repo_id,
        "job_type": schedule.job_type,
        "cron_expr": schedule.cron_expr,
        "enabled": schedule.enabled,
        "chain_export": _chain_name(schedule.chain_export),
    }


def _values_from(form: FormData) -> dict[str, Any]:
    """The submitted fields, to show them again next to an error."""
    try:
        repo_id: int | None = int(str(form.get("repo_id") or 0))
    except ValueError:
        repo_id = None
    return {
        "repo_id": repo_id,
        "job_type": str(form.get("job_type") or "sync"),
        "cron_expr": str(form.get("cron_expr") or ""),
        "enabled": bool(form.get("enabled")),
        "chain_export": str(form.get("chain_export") or "inherit"),
    }


@dataclass
class _Fields:
    repo: Repository
    job_type: str
    cron: str
    preset_label: str
    enabled: bool
    chain: bool | None


def _parse(db: Session, form: FormData) -> _Fields | str:
    """Validate the schedule form; return the fields or an error message."""
    try:
        repo_id = int(str(form.get("repo_id") or 0))
    except ValueError:
        return "select a valid repository"

    cron = str(form.get("cron_expr") or "").strip()
    error = validators.validate_cron(cron)
    if error or repo_id <= 0:
        return error or "select a repository"

    repo = repos.get(db, repo_id)
    if repo is None:
        return "unknown repository"

    job_type = str(form.get("job_type") or "sync")
    if job_type not in SCHEDULE_JOB_TYPES:
        return "job type must be sync, export or rehash"

    chain = _chain_choice(form.get("chain_export"))
    if isinstance(chain, str):
        return chain
    if job_type != "sync":
        chain = None  # only a sync can chain an export

    return _Fields(
        repo=repo,
        job_type=job_type,
        cron=cron,
        preset_label=_preset_for(cron),
        enabled=bool(form.get("enabled")),
        chain=chain,
    )


def _preset_for(cron: str) -> str:
    """The preset label (shown in the table) of an expression that equals a preset."""
    return next((key for key, (expr, _) in scheduler.PRESETS.items() if expr == cron), "")


def _cron_context(expr: str, helper: cronhelp.Helper | None = None) -> dict[str, Any]:
    """What schedules/_cron.html shows for an expression: helper, description, next runs."""
    expr = expr.strip()
    error = validators.validate_cron(expr)
    next_runs: list[datetime] = []
    if not error:
        run = utcnow()
        for _ in range(NEXT_RUNS):
            run = scheduler.next_after(expr, run)
            next_runs.append(run)
    return {
        "expr": expr,
        "helper": helper or cronhelp.parse(expr),
        "text": "" if error else cronhelp.describe(expr),
        "next_runs": next_runs,
        "error": error,
    }


def _context(db: Session, errors: list[str] | None, values: dict[str, Any]) -> dict[str, Any]:
    repositories = repos.list_all(db)
    return {
        "repositories": repositories,
        "repos_by_id": {repo.id: repo for repo in repositories},
        "errors": errors or [],
        "values": values,
        "cron": _cron_context(str(values.get("cron_expr") or "")),
        **CRON_CHOICES,
    }


def _page(
    request: Request,
    db: Session,
    errors: list[str] | None = None,
    values: dict[str, Any] | None = None,
) -> Response:
    context = _context(db, errors, values or DEFAULT_VALUES)
    context["schedules"] = list(db.exec(select(Schedule).order_by(col(Schedule.id))).all())
    return render(request, "schedules/list.html", context)


def _edit_page(
    request: Request,
    db: Session,
    schedule: Schedule,
    errors: list[str] | None = None,
    values: dict[str, Any] | None = None,
) -> Response:
    context = _context(db, errors, values or _values_of(schedule))
    context["schedule"] = schedule
    return render(request, "schedules/edit.html", context)


@router.get("/schedules")
def list_page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return _page(request, db)


@router.post("/schedules")
async def create(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    fields = _parse(db, form)
    if isinstance(fields, str):
        return _page(request, db, [fields], _values_from(form))

    schedule = Schedule(
        repo_id=fields.repo.id or 0,
        job_type=fields.job_type,
        cron_expr=fields.cron,
        preset_label=fields.preset_label,
        enabled=fields.enabled,
        chain_export=fields.chain,
        next_fire_at=scheduler.next_after(fields.cron),
    )
    db.add(schedule)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="schedule.create",
        target_type="schedule",
        target_id=fields.repo.slug,
        detail={"cron": fields.cron, "job_type": fields.job_type},
        commit=False,
    )
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/cron-helper")
async def cron_helper(
    request: Request,
    _user: CurrentUser = Depends(require_user),
) -> Response:
    """Re-render the cron field and its helper (htmx).

    A change in the helper (cb_source=helper) rebuilds the expression unless the
    helper is set to "custom"; an edited expression (cb_source=cron) is shown in
    the helper where it can be.
    """
    form = await request.form()
    expr = str(form.get("cron_expr") or "").strip()
    helper: cronhelp.Helper | None = None
    if form.get("cb_source") == "helper":
        helper = cronhelp.from_form(form, [str(day) for day in form.getlist("cb_day")])
        built = cronhelp.build(helper)
        if built is not None:
            expr = built
            helper = None  # show the expression as parsed (e.g. no weekday ticked → Monday)
    return render(
        request,
        "schedules/_cron.html",
        {"cron": _cron_context(expr, helper), **CRON_CHOICES},
    )


@router.get("/schedules/{schedule_id}/edit")
def edit_page(
    schedule_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        return render(request, "error.html", {"status": 404, "detail": "Schedule not found"}, 404)
    return _edit_page(request, db, schedule)


@router.post("/schedules/{schedule_id}")
async def update(
    schedule_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        return _page(request, db, ["unknown schedule"])

    form = await request.form()
    fields = _parse(db, form)
    if isinstance(fields, str):
        return _edit_page(request, db, schedule, [fields], _values_from(form))

    before = _values_of(schedule)
    schedule.repo_id = fields.repo.id or 0
    schedule.job_type = fields.job_type
    schedule.cron_expr = fields.cron
    schedule.enabled = fields.enabled
    schedule.chain_export = fields.chain
    schedule.preset_label = fields.preset_label
    schedule.next_fire_at = scheduler.next_after(fields.cron)
    db.add(schedule)
    after = _values_of(schedule)
    changes = {key: [before[key], after[key]] for key in after if before[key] != after[key]}

    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="schedule.update",
        target_type="schedule",
        target_id=str(schedule_id),
        detail={"cron": fields.cron, "repo": fields.repo.slug, "changes": changes},
        commit=False,
    )
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete(
    schedule_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is not None:
        audit.record(
            db,
            actor=user.admin.username,
            ip=client_ip(request),
            action="schedule.delete",
            target_type="schedule",
            target_id=str(schedule_id),
            detail={"cron": schedule.cron_expr},
            commit=False,
        )
        db.delete(schedule)
        db.commit()

    return RedirectResponse("/schedules", status_code=303)
