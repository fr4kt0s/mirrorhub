"""Runtime settings page (log retention, disk thresholds)."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.services import appsettings, audit
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


@router.get("/settings")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return render(
        request,
        "settings.html",
        {"values": appsettings.snapshot(db), "env": get_settings(), "errors": []},
    )


@router.post("/settings")
async def save(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    errors: list[str] = []
    try:
        retention = int(str(form.get("log_retention_days") or ""))
        warn = float(str(form.get("disk_warn_percent") or ""))
        critical = float(str(form.get("disk_critical_percent") or ""))
    except ValueError:
        return render(
            request,
            "settings.html",
            {"values": appsettings.snapshot(db), "env": get_settings(), "errors": ["numbers only"]},
            400,
        )
    if not 1 <= retention <= 3650:
        errors.append("log retention must be 1-3650 days")
    if not 1 <= warn < critical <= 100:
        errors.append("disk thresholds must satisfy 1 <= warn < critical <= 100")
    if errors:
        return render(
            request,
            "settings.html",
            {"values": appsettings.snapshot(db), "env": get_settings(), "errors": errors},
            400,
        )

    appsettings.set_value(db, "log_retention_days", retention, commit=False)
    appsettings.set_value(db, "disk_warn_percent", warn, commit=False)
    appsettings.set_value(db, "disk_critical_percent", critical, commit=False)
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="settings.update",
        target_type="setting",
        target_id="runtime",
        detail={
            "log_retention_days": retention,
            "disk_warn_percent": warn,
            "disk_critical_percent": critical,
        },
        commit=False,
    )
    db.commit()
    return RedirectResponse("/settings", status_code=303)
