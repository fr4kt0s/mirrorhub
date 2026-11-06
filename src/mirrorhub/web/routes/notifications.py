"""Notification channels and rules."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.enums import NotifyEvent
from mirrorhub.services import audit, notify, repos
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


def _page(
    request: Request, db: Session, errors: list[str] | None = None, results: list[str] | None = None
) -> Response:
    channels = notify.list_channels(db)
    return render(
        request,
        "notifications.html",
        {
            "channels": [
                {
                    **channel.model_dump(exclude={"config"}),
                    "url": (channel.config or {}).get("url", ""),
                    "format": (channel.config or {}).get("format", ""),
                }
                for channel in channels
            ],
            "rules": notify.list_rules(db),
            "repositories": repos.list_all(db),
            "events": [event.value for event in NotifyEvent],
            "errors": errors or [],
            "results": results or [],
        },
    )


@router.get("/notifications")
def page_get(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return _page(request, db)


@router.post("/notifications/channels")
async def create_channel(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    name = str(form.get("name") or "").strip()
    channel_type = str(form.get("type") or "")
    if not name:
        return _page(request, db, ["channel name is required"])
    if channel_type == "smtp":
        config = {
            "host": str(form.get("host") or ""),
            "port": int(str(form.get("port") or 587)),
            "username": str(form.get("username") or ""),
            "password": str(form.get("password") or ""),
            "from": str(form.get("from") or ""),
            "to": str(form.get("to") or ""),
            "tls": bool(form.get("tls")),
        }
    elif channel_type == "webhook":
        config = {
            "url": str(form.get("webhook_url") or ""),
            "format": str(form.get("format") or "generic"),
            "token": str(form.get("token") or ""),
        }
    else:
        return _page(request, db, ["channel type must be smtp or webhook"])
    try:
        notify.create_channel(
            db,
            name=name,
            channel_type=channel_type,
            config=config,
            actor=user.admin.username,
            ip=client_ip(request),
        )
    except notify.NotifyError as exc:
        return _page(request, db, [str(exc)])
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/channels/{channel_id}/delete")
def delete_channel(
    channel_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    channel = db.get(
        __import__("mirrorhub.models", fromlist=["NotificationChannel"]).NotificationChannel,
        channel_id,
    )
    if channel is not None:
        notify.delete_channel(db, channel, actor=user.admin.username, ip=client_ip(request))
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/channels/{channel_id}/test")
def test_channel(
    channel_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    channel = db.get(
        __import__("mirrorhub.models", fromlist=["NotificationChannel"]).NotificationChannel,
        channel_id,
    )
    if channel is None:
        return _page(request, db, ["unknown channel"])
    delivery = notify.test_channel(db, channel)
    detail = "sent" if delivery.ok else f"failed: {delivery.detail}"
    audit.record(
        db,
        actor=user.admin.username,
        ip=client_ip(request),
        action="notify.test",
        target_type="notification_channel",
        target_id=channel.name,
        detail={"ok": delivery.ok, "detail": detail},
    )
    return _page(request, db, results=[f"{delivery.channel}: {detail}"])


@router.post("/notifications/rules")
async def create_rule(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    channel_id = int(str(form.get("channel_id") or 0))
    event = str(form.get("event") or "")
    repo_raw = str(form.get("repo_id") or "")
    repo_id = int(repo_raw) if repo_raw.isdigit() and int(repo_raw) > 0 else None
    try:
        notify.create_rule(
            db,
            channel_id=channel_id,
            event=event,
            repo_id=repo_id,
            actor=user.admin.username,
            ip=client_ip(request),
        )
    except notify.NotifyError as exc:
        return _page(request, db, [str(exc)])
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/rules/{rule_id}/delete")
def delete_rule(
    rule_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    rule = db.get(
        __import__("mirrorhub.models", fromlist=["NotificationRule"]).NotificationRule, rule_id
    )
    if rule is not None:
        notify.delete_rule(db, rule, actor=user.admin.username, ip=client_ip(request))
    return RedirectResponse("/notifications", status_code=303)
