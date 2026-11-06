"""Repository list, create, edit, delete."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from mirrorhub.models import Repository
from mirrorhub.services import presets, repos
from mirrorhub.web.deps import CurrentUser, client_ip, get_db, render, require_user

router = APIRouter()


@router.get("/repos")
def list_page(
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return render(request, "repos/list.html", {"repos": repos.list_all(db), "errors": []})


def _blank_values() -> dict[str, str]:
    values = dict.fromkeys(repos.TEXT_FIELDS, "")
    values.update(dict.fromkeys(repos.LIST_FIELDS, ""))
    values.update(dict.fromkeys(repos.BOOL_FIELDS, "on"))
    values.update(
        {"enabled": "on", "verify_signatures": "on", "bwlimit_kbps": "0", "export_split_mb": "0"}
    )
    values.update(
        {
            "extra_files": "",
            "retry_max_attempts": "3",
            "retry_backoff": "exponential",
            "retry_base_seconds": "60",
            "retry_max_seconds": "3600",
            "retry_exit_codes": "1,2,10,11,12,20,23,24,30,35,255",
            "sig_off_confirm": "",
            "save_as_preset": "",
            "preset_name": "",
        }
    )
    return values


@router.get("/repos/new")
def new_page(
    request: Request,
    preset: int = 0,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    values = _blank_values()
    values.update(
        {
            "kind": "apt",
            "tool": "debmirror",
            "method": "https",
            "diff_mode": "use",
            "cleanup_mode": "post",
            "export_mode": "incr",
            "export_format": "tar.zst",
        }
    )
    if preset:
        row = presets.get(db, preset)
        if row is not None:
            values.update(repos.preset_values(row.config))
            values["preset_source"] = row.name
    return render(
        request, "repos/form.html", {"values": values, "errors": [], "repo": None, "mode": "create"}
    )


@router.get("/repos/{repo_id}/edit")
def edit_page(
    repo_id: int,
    request: Request,
    _user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    repo = repos.get(db, repo_id)
    if repo is None:
        return render(request, "error.html", {"status": 404, "detail": "Repository not found"}, 404)
    values = _blank_repo_values(repo)
    return render(
        request, "repos/form.html", {"values": values, "errors": [], "repo": repo, "mode": "edit"}
    )


def _blank_repo_values(repo: Repository) -> dict[str, str]:
    values = _blank_values()
    values.update(repos.preset_values(presets.config_for_repo(repo)))
    values["slug"] = repo.slug
    values["name"] = repo.name
    values["extra_args"] = ", ".join(repo.extra_args)
    return values


async def _form_data(form) -> tuple[dict, dict]:
    data, raw = repos.parse_form(form)
    return data, repos.form_values(data, raw)


async def _save(
    request: Request,
    db: Session,
    user: CurrentUser,
    repo: Repository | None,
) -> Response:
    form = await request.form()
    try:
        data, values = await _form_data(form)
    except repos.FormError as exc:
        values = {name: str(form.get(name) or "") for name in form}
        return render(
            request,
            "repos/form.html",
            {
                "values": {**_blank_values(), **values},
                "errors": exc.messages,
                "repo": repo,
                "mode": "edit" if repo else "create",
            },
            400,
        )
    values = {**_blank_values(), **values}
    if repo is not None:
        data["slug"] = repo.slug
        values["slug"] = repo.slug
    errors = repos.validate(data)

    slug_error = repos._ensure_unique_slug(db, data["slug"], repo.id if repo else None)
    if slug_error:
        errors.append(slug_error)

    disabling = not data["verify_signatures"] and (repo is None or repo.verify_signatures)
    phrase = str(form.get("sig_off_confirm") or "").strip()
    if disabling and phrase != repos.SIG_OFF_PHRASE:
        errors.append(
            f"signature checking: type '{repos.SIG_OFF_PHRASE}' to disable it for this repository"
        )

    if errors:
        return render(
            request,
            "repos/form.html",
            {
                "values": values,
                "errors": errors,
                "repo": repo,
                "mode": "edit" if repo else "create",
            },
            400,
        )

    actor = user.admin.username
    ip = client_ip(request)
    if repo is None:
        saved = repos.create(db, data, actor=actor, ip=ip)
    else:
        saved = repos.update(db, repo, data, actor=actor, ip=ip)

    preset_name = str(form.get("preset_name") or "").strip()
    if form.get("save_as_preset") and preset_name:
        try:
            presets.save_from_repo(db, saved, preset_name, actor=actor, ip=ip, overwrite=False)
        except ValueError as exc:
            errors.append(str(exc))
            return render(
                request,
                "repos/form.html",
                {"values": values, "errors": errors, "repo": saved, "mode": "edit"},
                400,
            )
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos")
async def create_repo(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    return await _save(request, db, user, None)


@router.post("/repos/{repo_id}")
async def update_repo(
    repo_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    repo = repos.get(db, repo_id)
    if repo is None:
        return render(request, "error.html", {"status": 404, "detail": "Repository not found"}, 404)
    return await _save(request, db, user, repo)


@router.post("/repos/{repo_id}/delete")
def delete_repo(
    repo_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    repo = repos.get(db, repo_id)
    if repo is None:
        return Response("not found", status_code=404)
    blockers = repos.delete(db, repo, actor=user.admin.username, ip=client_ip(request))
    if blockers:
        return render(
            request,
            "repos/list.html",
            {"repos": repos.list_all(db), "errors": blockers},
            409,
        )
    return RedirectResponse("/repos", status_code=303)
