"""Config-as-code: export and import repository and schedule definitions as YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlmodel import Session, col, select

from mirrorhub import __version__
from mirrorhub.models import Repository, Schedule, default_retry_policy, utcnow
from mirrorhub.security import validators
from mirrorhub.services import audit, repos

ROOT_KEY = "mirrorhub_config"
VERSION = 1

REPO_FIELDS: tuple[str, ...] = (
    "slug",
    "name",
    "kind",
    "tool",
    "enabled",
    "upstream_host",
    "upstream_root",
    "method",
    "suites",
    "sections",
    "architectures",
    "include_source",
    "rsync_extra",
    "extra_files",
    "bwlimit_kbps",
    "diff_mode",
    "cleanup_mode",
    "verify_signatures",
    "keyring_name",
    "include_patterns",
    "exclude_patterns",
    "extra_args",
    "retry_policy",
    "export_mode",
    "export_format",
    "export_split_mb",
    "chain_export",
)
SCHEDULE_FIELDS: tuple[str, ...] = (
    "repo_slug",
    "job_type",
    "cron_expr",
    "preset_label",
    "enabled",
    "chain_export",
)


def _defaults() -> dict[str, Any]:
    """Defaults applied to entries that omit optional keys, so short YAML stays valid."""
    return {
        "enabled": True,
        "kind": "apt",
        "tool": "debmirror",
        "method": "https",
        "suites": [],
        "sections": [],
        "architectures": ["none"],
        "include_source": False,
        "rsync_extra": [],
        "extra_files": [],
        "bwlimit_kbps": 0,
        "diff_mode": "use",
        "cleanup_mode": "post",
        "verify_signatures": True,
        "keyring_name": "trustedkeys.gpg",
        "include_patterns": [],
        "exclude_patterns": [],
        "extra_args": [],
        "retry_policy": default_retry_policy(),
        "export_mode": "incr",
        "export_format": "tar.zst",
        "export_split_mb": 0,
        "chain_export": False,
    }


class ConfigError(ValueError):
    pass


@dataclass
class ImportPlan:
    creates: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    schedules: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.schedules)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _repo_dict(repo: Repository) -> dict[str, Any]:
    return {name: getattr(repo, name) for name in REPO_FIELDS}


def export_document(db: Session) -> dict[str, Any]:
    repo_rows = repos.list_all(db)
    by_id = {repo.id: repo.slug for repo in repo_rows}
    schedules = db.exec(select(Schedule).order_by(col(Schedule.id))).all()
    return {
        ROOT_KEY: VERSION,
        "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
        "generator": f"mirrorhub {__version__}",
        "repositories": [_repo_dict(repo) for repo in repo_rows],
        "schedules": [
            {
                "repo_slug": by_id.get(schedule.repo_id, ""),
                "job_type": schedule.job_type,
                "cron_expr": schedule.cron_expr,
                "preset_label": schedule.preset_label,
                "enabled": schedule.enabled,
                "chain_export": schedule.chain_export,
            }
            for schedule in schedules
            if schedule.repo_id in by_id
        ],
    }


def export_yaml(db: Session) -> str:
    return str(yaml.safe_dump(export_document(db), sort_keys=False, allow_unicode=True))


def parse_yaml(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML syntax error: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("YAML root must be a mapping")
    if document.get(ROOT_KEY) != VERSION:
        raise ConfigError(f"expected '{ROOT_KEY}: {VERSION}' at the top level")
    entries = document.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("'repositories' must be a non-empty list")
    return document


def build_plan(
    db: Session,
    document: dict[str, Any],
    *,
    overwrite: bool = False,
    allow_disable_signatures: bool = False,
) -> ImportPlan:
    plan = ImportPlan()
    seen: set[str] = set()

    for index, entry in enumerate(document.get("repositories") or []):
        if not isinstance(entry, dict):
            plan.errors.append(f"repositories[{index}]: entry must be a mapping")
            continue
        data: dict[str, Any] = _defaults()
        data.update({name: entry[name] for name in REPO_FIELDS if name in entry})
        for list_field in (
            "suites",
            "sections",
            "architectures",
            "rsync_extra",
            "include_patterns",
            "exclude_patterns",
            "extra_args",
        ):
            data[list_field] = _as_list(data[list_field])
        if not isinstance(data["retry_policy"], dict):
            data["retry_policy"] = default_retry_policy()

        if data["tool"] == "rsync":
            data["method"] = "rsync"

        slug = str(data.get("slug") or "")
        if slug in seen:
            plan.errors.append(f"repositories[{index}]: duplicate slug '{slug}' in the document")
            continue
        seen.add(slug)
        missing = [
            name
            for name in ("slug", "name", "kind", "tool", "method", "upstream_host", "upstream_root")
            if not data.get(name)
        ]
        if missing:
            plan.errors.append(
                f"repositories[{index}] ({slug or 'no slug'}): missing {', '.join(missing)}"
            )
            continue
        errors = repos.validate(data)
        if errors:
            plan.errors.extend(f"repositories[{index}] ({slug}): {error}" for error in errors)
            continue
        if data.get("verify_signatures") is False and not allow_disable_signatures:
            plan.errors.append(
                f"repositories[{index}] ({slug}): signature checking is disabled in the file; "
                "tick 'Allow disabling signature checking' to import it"
            )
            continue

        existing = db.exec(select(Repository).where(Repository.slug == slug)).first()
        if existing is None:
            plan.creates.append(data)
        elif overwrite:
            plan.updates.append(data)
        else:
            plan.conflicts.append(slug)

    for index, entry in enumerate(document.get("schedules") or []):
        if not isinstance(entry, dict):
            plan.errors.append(f"schedules[{index}]: entry must be a mapping")
            continue
        slug = str(entry.get("repo_slug", ""))
        cron = str(entry.get("cron_expr", ""))
        if not slug:
            plan.errors.append(f"schedules[{index}]: repo_slug is required")
            continue
        error = validators.validate_cron(cron)
        if error:
            plan.errors.append(f"schedules[{index}] ({slug}): {error}")
            continue
        plan.schedules.append(
            {
                "repo_slug": slug,
                "job_type": str(entry.get("job_type", "sync")),
                "cron_expr": cron,
                "preset_label": str(entry.get("preset_label", "")),
                "enabled": bool(entry.get("enabled", True)),
                "chain_export": bool(entry.get("chain_export", False)),
            }
        )
    return plan


def apply_plan(db: Session, plan: ImportPlan, *, actor: str, ip: str = "") -> ImportPlan:
    known = {repo.slug for repo in repos.list_all(db)}
    for data in plan.creates:
        repos.create(db, data, actor=actor, ip=ip)
        known.add(data["slug"])
    for data in plan.updates:
        existing = db.exec(select(Repository).where(Repository.slug == data["slug"])).first()
        if existing is not None:
            repos.update(db, existing, data, actor=actor, ip=ip)
    for entry in plan.schedules:
        if entry["repo_slug"] not in known:
            plan.errors.append(f"schedules: unknown repository '{entry['repo_slug']}'")
            continue
        repo = db.exec(select(Repository).where(Repository.slug == entry["repo_slug"])).first()
        if repo is None:
            plan.errors.append(f"schedules: unknown repository '{entry['repo_slug']}'")
            continue
        schedule = db.exec(
            select(Schedule).where(
                Schedule.repo_id == repo.id, col(Schedule.job_type) == entry["job_type"]
            )
        ).first()
        if schedule is None:
            db.add(
                Schedule(
                    repo_id=repo.id or 0,
                    job_type=entry["job_type"],
                    cron_expr=entry["cron_expr"],
                    preset_label=entry["preset_label"],
                    enabled=entry["enabled"],
                    chain_export=entry["chain_export"],
                )
            )
        else:
            schedule.cron_expr = entry["cron_expr"]
            schedule.preset_label = entry["preset_label"]
            schedule.enabled = entry["enabled"]
            schedule.chain_export = entry["chain_export"]
            db.add(schedule)
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="config.import",
        target_type="config",
        target_id="yaml",
        detail={
            "created": [entry["slug"] for entry in plan.creates],
            "updated": [entry["slug"] for entry in plan.updates],
            "schedules": len(plan.schedules),
            "conflicts": plan.conflicts,
        },
        commit=False,
    )
    db.commit()
    return plan
