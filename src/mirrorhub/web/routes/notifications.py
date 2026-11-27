"""Notification channels and rules."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.enums import ChannelType, NotifyEvent
from mirrorhub.models import NotificationChannel, NotificationRule
from mirrorhub.services import audit, notify, repos
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


def _page(
    request: Request, db: Session, errors: list[str] | None = None, results: list[str] | None = None
) -> Response:
    channels = notify.list_channels(db)
    repositories = repos.list_all(db)
    return render(
        request,
        "notifications.html",
        {
            "channels": [
                {
                    **channel.model_dump(exclude={"config"}),
                    "target": notify.describe_channel(channel),
                }
                for channel in channels
            ],
            "channel_names": {channel.id: channel.name for channel in channels},
            "repo_names": {repo.id: repo.slug for repo in repositories},
            "rules": notify.list_rules(db),
            "deliveries": notify.recent_deliveries(db),
            "repositories": repositories,
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
    """Show channels, rules and recent deliveries."""
    return _page(request, db)


@router.post("/notifications/channels")
async def create_channel(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Create a notification channel from the form."""
    form = await request.form()
    name = str(form.get("name") or "").strip()
    channel_type = str(form.get("type") or "")
    if not name:
        return _page(request, db, ["channel name is required"])
    config: dict[str, object]
    if channel_type == ChannelType.SMTP.value:
        config = {
            "host": str(form.get("host") or ""),
            "port": str(form.get("port") or ""),
            "username": str(form.get("username") or ""),
            "password": str(form.get("password") or ""),
            "from": str(form.get("from") or ""),
            "to": str(form.get("to") or ""),
            "tls": bool(form.get("tls")),
        }
    elif channel_type == ChannelType.WEBHOOK.value:
        config = {
            "url": str(form.get("webhook_url") or ""),
            "format": str(form.get("format") or "generic"),
            "token": str(form.get("token") or ""),
        }
    elif channel_type == ChannelType.FILE.value:
        config = {
            "log_mode": str(form.get("log_mode") or "tail"),
            "log_lines": str(form.get("log_lines") or ""),
            "retention_days": str(form.get("retention_days") or ""),
        }
    else:
        return _page(request, db, ["channel type must be smtp, webhook or file"])
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
    """Delete a channel with its rules and queued deliveries."""
    channel = db.get(NotificationChannel, channel_id)
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
    """Send a test message through a channel."""
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        return _page(request, db, ["unknown channel"])
    delivery = notify.test_channel(db, channel)
    if delivery.ok:
        detail = f"written: {delivery.detail}" if delivery.detail else "sent"
    else:
        detail = f"failed: {delivery.detail}"
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
    """Create a rule that sends an event to a channel."""
    form = await request.form()
    channel_raw = str(form.get("channel_id") or "")
    if not channel_raw.isdigit():
        return _page(request, db, ["choose a channel"])
    channel_id = int(channel_raw)
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
    """Delete a rule."""
    rule = db.get(NotificationRule, rule_id)
    if rule is not None:
        notify.delete_rule(db, rule, actor=user.admin.username, ip=client_ip(request))
    return RedirectResponse("/notifications", status_code=303)
