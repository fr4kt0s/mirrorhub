"""Repository CRUD: form parsing, validation, persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session, col, select

from mirrorhub.enums import (
    CleanupMode,
    DiffMode,
    JobStatus,
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


def parse_extra_files(raw: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if "->" not in text:
            raise FormError([f"extra files: expected 'URL -> relative/path', got {text[:60]!r}"])
        url, path = (part.strip() for part in text.split("->", 1))
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

    root = data["upstream_root"]
    if root or tool != "wget":
        root_error = validators.validate_root(root)
        if root_error:
            errors.append(root_error)

    for check in (
        validators.validate_slug(data["slug"]),
        validators.validate_name(data["name"]),
        validators.validate_host(data["upstream_host"]),
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
        value = data[field]
        if len(value) > 500:
            errors.append(f"{field.replace('_', ' ')}: at most 500 characters")
        if "\n" in value or "\r" in value:
            errors.append(f"{field.replace('_', ' ')}: newlines are not allowed")

    if data["kind"] not in RepoKind:
        errors.append("type: must be apt, arch or generic")
    if tool not in Tool or tool == Tool.EXPORT:
        errors.append("tool: must be debmirror, rsync or wget")
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
                errors.append("Arch repositories: at least one repository is required (core, extra, ...)")
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

    return errors


def _ensure_unique_slug(db: Session, slug: str, repo_id: int | None) -> str | None:
    existing = db.exec(select(Repository).where(Repository.slug == slug)).first()
    if existing is not None and existing.id != repo_id:
        return f"slug: '{slug}' is already used by another repository"
    return None


def create(db: Session, data: dict[str, Any], *, actor: str, ip: str = "") -> Repository:
    repo = Repository(**{field: data[field] for field in DATA_FIELDS})
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
        if field == "slug":
            continue
        setattr(repo, field, data[field])
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
    from mirrorhub.models import ManifestSnapshot, NotificationRule, RepoLock, Schedule, StorageCache

    for model in (NotificationRule, Export, Schedule, RepoLock, StorageCache, ManifestSnapshot):
        rows = db.exec(select(model).where(model.repo_id == repo.id)).all()
        for row in rows:
            db.delete(row)

    jobs = db.exec(
        select(Job).where(Job.repo_id == repo.id).order_by(col(Job.id).desc())
    ).all()
    for job in jobs:
        db.delete(job)

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


def active_job_statuses() -> tuple[str, ...]:
    return (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.CANCELLING.value)
