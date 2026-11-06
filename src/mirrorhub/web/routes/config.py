"""Config-as-code page: YAML download, import preview and apply."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from mirrorhub.services import yamlio
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()
MAX_YAML_BYTES = 1024 * 1024


@router.get("/config")
def page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return render(
        request,
        "config.html",
        {"yaml_text": yamlio.export_yaml(db), "plan": None, "applied": False},
    )


@router.get("/config/export.yaml")
def download(
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return PlainTextResponse(
        yamlio.export_yaml(db),
        media_type="application/x-yaml",
        headers={"Content-Disposition": 'attachment; filename="mirrorhub-config.yaml"'},
    )


@router.post("/config/import")
async def import_config(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    text = str(form.get("yaml_text") or "")
    overwrite = bool(form.get("overwrite"))
    allow_disable = bool(form.get("allow_disable_signatures"))
    apply_now = bool(form.get("apply"))

    if len(text.encode()) > MAX_YAML_BYTES:
        return render(
            request,
            "config.html",
            {
                "yaml_text": "",
                "plan": None,
                "applied": False,
                "errors": ["YAML input is larger than 1 MB"],
            },
            413,
        )
    try:
        document = yamlio.parse_yaml(text)
    except yamlio.ConfigError as exc:
        return render(
            request,
            "config.html",
            {"yaml_text": text, "plan": None, "applied": False, "errors": [str(exc)]},
            400,
        )

    plan = yamlio.build_plan(
        db, document, overwrite=overwrite, allow_disable_signatures=allow_disable
    )
    if apply_now:
        if plan.errors:
            return render(
                request,
                "config.html",
                {"yaml_text": text, "plan": plan, "applied": False},
                400,
            )
        yamlio.apply_plan(db, plan, actor=user.admin.username, ip=client_ip(request))
        return render(
            request,
            "config.html",
            {"yaml_text": yamlio.export_yaml(db), "plan": plan, "applied": True},
        )
    return render(
        request,
        "config.html",
        {
            "yaml_text": text,
            "plan": plan,
            "applied": False,
            "overwrite": overwrite,
            "allow_disable": allow_disable,
        },
    )
