"""Repository CRUD: form parsing, validation, persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, col, select

from mirrorhub.enums import (
    CleanupMode,
    DiffMode,
    Method,
    RepoKind,
    Tool,
)
from mirrorhub.models import Export, Job, Repository, default_retry_policy
from mirrorhub.security import validators
from mirrorhub.services import audit

BOOL_FIELDS = ("enabled", "include_source", "verify_signatures", "chain_export")
LIST_FIELDS = (
    "suites",
    "sections",
    "architectures",
    "arch_exclude",
    "rsync_extra",
    "include_patterns",
    "exclude_patterns",
    "extra_args",
)
TEXT_FIELDS = (
    "slug",
    "name",
    "kind",
    "tool",
    "method",
    "upstream_host",
    "upstream_root",
    "include_regex",
    "exclude_regex",
    "diff_mode",
    "cleanup_mode",
    "export_mode",
    "export_format",
)
INT_FIELDS = ("bwlimit_kbps", "export_split_mb")
SIG_OFF_PHRASE = "I understand"

DATA_FIELDS = (
    *TEXT_FIELDS,
    *LIST_FIELDS,
    *BOOL_FIELDS,
    *INT_FIELDS,
    "extra_files",
    "retry_policy",
)


class FormError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def _split(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]


def _split_architectures(raw: str) -> list[str]:
    return [part for part in re.split(r"[,\s]+", raw) if part.strip()]


WGET_MODES = ("tree", "files")


def wget_mode_for(extra_files: list[dict[str, str]] | None) -> str:
    """A wget repository with file entries downloads those files; otherwise the whole tree."""
    return "files" if extra_files else "tree"


def parse_extra_files(raw: str) -> list[dict[str, str]]:
    """One file per line: 'URL' (stored under the URL's path) or 'URL -> relative/path'."""
    items: list[dict[str, str]] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if "->" in text:
            url, path = (part.strip() for part in text.split("->", 1))
        else:
            url = text
            path = urlsplit(url).path.strip("/")
            if not path or url.split("?", 1)[0].endswith("/"):
                raise FormError(
                    [
                        f"file list: cannot derive a file name from {text[:60]!r}; "
                        "write 'URL -> relative/path'"
                    ]
                )
        items.append({"url": url, "path": path})
    return items


def parse_form(form: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (validated-shape data, raw strings for re-rendering the form)."""
    messages: list[str] = []
    data: dict[str, Any] = {}
    raw: dict[str, str] = {}

    def get(name: str) -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else ""

    for name in TEXT_FIELDS:
        raw[name] = get(name)
        data[name] = get(name)
    for name in LIST_FIELDS:
        raw[name] = get(name)
        data[name] = _split(get(name))
    for name in ("architectures", "arch_exclude"):
        data[name] = _split_architectures(get(name))
    for name in BOOL_FIELDS:
        data[name] = bool(form.get(name))
        raw[name] = "on" if data[name] else ""
    for name in INT_FIELDS:
        text = get(name) or "0"
        raw[name] = text
        try:
            data[name] = int(text)
        except ValueError:
            messages.append(f"{name.replace('_', ' ')}: must be a whole number")
            data[name] = 0

    raw["extra_files"] = get("extra_files")
    wget_mode = get("wget_mode")
    if data.get("tool") == "wget" and wget_mode:
        # The form switch decides; the hidden file list is ignored in tree mode.
        raw["wget_mode"] = wget_mode
        data["wget_mode"] = wget_mode
    if data.get("wget_mode") == "tree":
        data["extra_files"] = []
    else:
        try:
            data["extra_files"] = parse_extra_files(get("extra_files"))
        except FormError as exc:
            messages.extend(exc.messages)
            data["extra_files"] = []

    policy = default_retry_policy()
    for key, field in (
        ("max_attempts", "retry_max_attempts"),
        ("base_seconds", "retry_base_seconds"),
        ("max_seconds", "retry_max_seconds"),
    ):
        text = get(field)
        raw[field] = text
        if text:
            try:
                policy[key] = int(text)
            except ValueError:
                messages.append(f"{key.replace('_', ' ')}: must be a whole number")
    backoff = get("retry_backoff") or policy["backoff"]
    raw["retry_backoff"] = backoff
    policy["backoff"] = backoff
    codes = get("retry_exit_codes")
    raw["retry_exit_codes"] = codes
    if codes:
        parsed: list[int] = []
        for token in _split(codes):
            try:
                parsed.append(int(token))
            except ValueError:
                messages.append(f"retry: {token[:10]!r} is not an exit code")
        if parsed:
            policy["retryable_exit_codes"] = parsed
    data["retry_policy"] = policy

    if messages:
        raise FormError(messages)
    return data, raw


def form_values(data: dict[str, Any], raw: dict[str, str]) -> dict[str, str]:
    """Raw strings for the form plus list-ish fields rendered back as comma-separated text."""
    values = dict(raw)
    for name in LIST_FIELDS:
        values[name] = ", ".join(data.get(name) or [])
    values["extra_files"] = "\n".join(
        f"{item['url']} -> {item['path']}" for item in data.get("extra_files") or []
    )
    values["wget_mode"] = raw.get("wget_mode") or wget_mode_for(data.get("extra_files"))
    values["retry_exit_codes"] = ",".join(
        str(code) for code in (data.get("retry_policy") or {}).get("retryable_exit_codes", [])
    )
    return values


def preset_values(config: dict[str, Any]) -> dict[str, str]:
    """Reuse form_values for a preset definition (same dict shapes as parsed data)."""
    values: dict[str, str] = {name: str(config.get(name, "")) for name in TEXT_FIELDS}
    for name in LIST_FIELDS:
        values[name] = ", ".join(str(item) for item in config.get(name) or [])
    for name in BOOL_FIELDS:
        values[name] = "on" if config.get(name) else ""
    for name in INT_FIELDS:
        values[name] = str(config.get(name, 0))
    values["extra_files"] = "\n".join(
        f"{item['url']} -> {item['path']}" for item in config.get("extra_files") or []
    )
    values["wget_mode"] = wget_mode_for(config.get("extra_files"))
    policy = config.get("retry_policy") or default_retry_policy()
    values["retry_max_attempts"] = str(policy.get("max_attempts", 3))
    values["retry_backoff"] = str(policy.get("backoff", "exponential"))
    values["retry_base_seconds"] = str(policy.get("base_seconds", 60))
    values["retry_max_seconds"] = str(policy.get("max_seconds", 3600))
    values["retry_exit_codes"] = ",".join(
        str(code) for code in policy.get("retryable_exit_codes", [])
    )
    return values


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tool = data["tool"]
    method = data["method"]

    for check in (
        validators.validate_slug(data["slug"]),
        validators.validate_name(data["name"]),
        validators.validate_host(data["upstream_host"]),
        validators.validate_root(data["upstream_root"]),
        validators.validate_patterns("include patterns", data["include_patterns"]),
        validators.validate_patterns("exclude patterns", data["exclude_patterns"]),
        validators.validate_extra_args(tool, data["extra_args"]),
        validators.validate_bwlimit(data["bwlimit_kbps"]),
        validators.validate_retry_policy(data["retry_policy"]),
        validators.validate_export_fields(
            data["export_mode"], data["export_format"], data["export_split_mb"]
        ),
        validators.validate_extra_files(data["extra_files"]),
    ):
        if check:
            errors.append(check)

    for field in ("include_regex", "exclude_regex"):
        value = data.get(field, "")
        if len(value) > 500:
            errors.append(f"{field.replace('_', ' ')}: at most 500 characters")
        if "\n" in value or "\r" in value:
            errors.append(f"{field.replace('_', ' ')}: newlines are not allowed")

    if data["kind"] not in RepoKind:
        errors.append("type: must be apt, arch or generic")
    if tool not in Tool or tool == Tool.EXPORT:
        errors.append("tool: must be debmirror, rsync, wget or ftpsync")
        return errors
    if method not in Method:
        errors.append("method: http, https or rsync")

    if tool == "debmirror":
        if method not in ("http", "https"):
            errors.append("method: debmirror supports http and https only")
        if not data["suites"]:
            errors.append("suites: at least one suite or dist is required")
        if not data["sections"]:
            errors.append("sections/components: required")
        error = validators.validate_list_field("suites", data["suites"])
        if error:
            errors.append(error)
        error = validators.validate_list_field("sections", data["sections"])
        if error:
            errors.append(error)
        error = validators.validate_architectures(data["architectures"])
        if error:
            errors.append(error)
        error = validators.validate_list_field("rsync extra directories", data["rsync_extra"])
        if error:
            errors.append(error)
        if data["diff_mode"] not in DiffMode:
            errors.append("diff mode: use, mirror or none")
        if data["cleanup_mode"] not in CleanupMode:
            errors.append("cleanup mode: post, cleanup or none")

    elif tool == "rsync":
        if method != "rsync":
            errors.append("method: the rsync tool requires method rsync")
        if not data["upstream_root"].strip("/"):
            errors.append("root path: must start with the rsync module, e.g. /packages")
        if data["kind"] == "arch":
            if not data["suites"]:
                errors.append(
                    "Arch repositories: at least one repository is required (core, extra, ...)"
                )
            if not data["architectures"] or "none" in data["architectures"]:
                errors.append("Architectures: Arch rsync mirrors require concrete architectures")
            error = validators.validate_architectures(data["architectures"])
            if error:
                errors.append(error)
            error = validators.validate_list_field("Arch repositories", data["suites"])
            if error:
                errors.append(error)

    elif tool == "wget":
        if method not in ("http", "https"):
            errors.append("method: wget supports http and https only")
        mode = data.get("wget_mode") or wget_mode_for(data["extra_files"])
        if mode not in WGET_MODES:
            errors.append("download: choose the whole directory tree or a file list")
        elif mode == "files":
            if not data["extra_files"]:
                errors.append("file list: enter at least one file URL, one per line")
            paths = [item.get("path", "") for item in data["extra_files"]]
            duplicates = sorted({path for path in paths if paths.count(path) > 1})
            if duplicates:
                errors.append(
                    "file list: several entries write to the same path: "
                    + ", ".join(duplicates[:5])
                )

    elif tool == "ftpsync":
        if method != "rsync":
            errors.append("method: ftpsync requires method rsync")

        include = data["architectures"]
        exclude = data["arch_exclude"]

        if include and exclude:
            errors.append(
                "architecture selection: use either ARCH_INCLUDE or ARCH_EXCLUDE, not both"
            )

        # Both empty is valid: ftpsync then mirrors all architectures.
        if "none" in include or "none" in exclude:
            errors.append(
                "architecture selection: 'none' is not an ftpsync architecture; "
                "leave both fields empty to mirror all architectures"
            )
        if include:
            error = validators.validate_list_field("architectures to include", include)
            if error:
                errors.append(error)
        if exclude:
            error = validators.validate_list_field("architectures to exclude", exclude)
            if error:
                errors.append(error)

    return errors


# Tools that fix the repository kind; rsync keeps the kind chosen in the form.
TOOL_KINDS: dict[str, str] = {"debmirror": "apt", "ftpsync": "apt", "wget": "generic"}


def apply_tool_rules(data: dict[str, Any], current: Repository | None) -> None:
    """Derive tool-dependent fields: the kind, and signature checking (debmirror only)."""
    kind = TOOL_KINDS.get(str(data.get("tool") or ""))
    if kind is not None:
        data["kind"] = kind
    if data.get("tool") != "debmirror":
        data["verify_signatures"] = current.verify_signatures if current is not None else True


def slug_conflict(db: Session, slug: str, repo_id: int | None) -> str | None:
    """Error message if another repository already uses the slug."""
    existing = db.exec(select(Repository).where(Repository.slug == slug)).first()
    if existing is not None and existing.id != repo_id:
        return f"slug: '{slug}' is already used by another repository"
    return None


def create(db: Session, data: dict[str, Any], *, actor: str, ip: str = "") -> Repository:
    repo = Repository(**{field: data[field] for field in DATA_FIELDS if field in data})
    db.add(repo)
    db.flush()
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="repo.create",
        target_type="repository",
        target_id=repo.slug,
        detail={"kind": repo.kind, "tool": repo.tool, "architectures": repo.architectures},
        commit=False,
    )
    if not repo.verify_signatures:
        audit.record(
            db,
            actor=actor,
            ip=ip,
            action="repo.signature_checking_disabled",
            target_type="repository",
            target_id=repo.slug,
            detail={"stage": "create"},
            commit=False,
        )
    db.commit()
    db.refresh(repo)
    return repo


def update(
    db: Session, repo: Repository, data: dict[str, Any], *, actor: str, ip: str = ""
) -> Repository:
    before = {field: getattr(repo, field) for field in ("slug", "name", "verify_signatures")}
    for field in DATA_FIELDS:
        if field == "slug" or field not in data:
            continue
        setattr(repo, field, data[field])
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="repo.update",
        target_type="repository",
        target_id=repo.slug,
        detail={"before": before, "after": {"name": repo.name, "slug": repo.slug}},
        commit=False,
    )
    if before["verify_signatures"] and not repo.verify_signatures:
        audit.record(
            db,
            actor=actor,
            ip=ip,
            action="repo.signature_checking_disabled",
            target_type="repository",
            target_id=repo.slug,
            detail={"stage": "update"},
            commit=False,
        )
    db.commit()
    db.refresh(repo)
    return repo


def deletion_blockers(db: Session, repo_id: int) -> list[str]:
    blockers: list[str] = []
    jobs = db.exec(
        select(Job).where(
            Job.repo_id == repo_id, col(Job.status).in_(["queued", "running", "cancelling"])
        )
    ).all()
    if jobs:
        blockers.append(f"{len(jobs)} queued or running job(s) reference this repository")
    return blockers


def delete(db: Session, repo: Repository, *, actor: str, ip: str = "") -> list[str]:
    blockers = deletion_blockers(db, repo.id or 0)
    if blockers:
        return blockers
    from mirrorhub.models import (
        ManifestSnapshot,
        NotificationRule,
        RepoLock,
        Schedule,
        StorageCache,
    )

    for model in (NotificationRule, Export, Schedule, RepoLock, StorageCache, ManifestSnapshot):
        rows = db.exec(select(model).where(model.repo_id == repo.id)).all()
        for row in rows:
            db.delete(row)

    jobs = db.exec(select(Job).where(Job.repo_id == repo.id).order_by(col(Job.id).desc())).all()
    for row in jobs:
        db.delete(row)

    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="repo.delete",
        target_type="repository",
        target_id=repo.slug,
        detail={"name": repo.name},
        commit=False,
    )
    db.delete(repo)
    db.commit()
    return []


def get(db: Session, repo_id: int) -> Repository | None:
    return db.get(Repository, repo_id)


def list_all(db: Session) -> list[Repository]:
    return list(db.exec(select(Repository).order_by(col(Repository.slug))).all())
