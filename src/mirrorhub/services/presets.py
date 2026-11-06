"""Presets: editable seed data, cloned into repositories. Never hard-coded behaviour."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from mirrorhub.models import Preset, Repository, default_retry_policy
from mirrorhub.services import audit

DEBIAN_SECTIONS = ["main", "contrib", "non-free", "non-free-firmware"]
UBUNTU_SECTIONS = ["main", "restricted", "universe", "multiverse"]

SEED_PRESETS: list[dict[str, Any]] = [
    {
        "name": "Debian 12 (bookworm)",
        "kind": "apt",
        "config": {
            "kind": "apt",
            "tool": "debmirror",
            "method": "https",
            "upstream_host": "deb.debian.org",
            "upstream_root": "/debian",
            "suites": ["bookworm", "bookworm-updates", "bookworm-backports"],
            "sections": DEBIAN_SECTIONS,
            "architectures": ["amd64"],
            "include_source": False,
            "diff_mode": "use",
            "cleanup_mode": "post",
            "verify_signatures": True,
            "rsync_extra": [],
            "include_patterns": [],
            "exclude_patterns": [],
            "extra_args": [],
            "bwlimit_kbps": 0,
            "export_mode": "incr",
            "export_format": "tar.zst",
            "export_split_mb": 0,
            "chain_export": False,
            "enabled": True,
        },
    },
    {
        "name": "Ubuntu 24.04 LTS (noble)",
        "kind": "apt",
        "config": {
            "kind": "apt",
            "tool": "debmirror",
            "method": "https",
            "upstream_host": "archive.ubuntu.com",
            "upstream_root": "/ubuntu",
            "suites": ["noble", "noble-updates", "noble-security"],
            "sections": UBUNTU_SECTIONS,
            "architectures": ["amd64"],
            "include_source": False,
            "diff_mode": "use",
            "cleanup_mode": "post",
            "verify_signatures": True,
            "rsync_extra": [],
            "include_patterns": [],
            "exclude_patterns": [],
            "extra_args": [],
            "bwlimit_kbps": 0,
            "export_mode": "incr",
            "export_format": "tar.zst",
            "export_split_mb": 0,
            "chain_export": False,
            "enabled": True,
        },
    },
    {
        "name": "Arch Linux (x86_64)",
        "kind": "arch",
        "config": {
            "kind": "arch",
            "tool": "rsync",
            "method": "rsync",
            "upstream_host": "geo.mirror.pkgbuild.com",
            "upstream_root": "/",
            "suites": ["core", "extra", "multilib"],
            "sections": [],
            "architectures": ["x86_64"],
            "include_source": False,
            "diff_mode": "none",
            "cleanup_mode": "post",
            "verify_signatures": True,
            "rsync_extra": [],
            "include_patterns": [],
            "exclude_patterns": [],
            "extra_args": [],
            "bwlimit_kbps": 0,
            "export_mode": "incr",
            "export_format": "tar.zst",
            "export_split_mb": 0,
            "chain_export": False,
            "enabled": True,
        },
    },
]

# Debian security lives on security.debian.org; the preset note tells the operator to
# add a second repository for bookworm-security rather than mixing hosts.


def seed(db: Session) -> int:
    """Insert missing seed presets. Idempotent; existing presets are never overwritten."""
    created = 0
    for item in SEED_PRESETS:
        existing = db.exec(select(Preset).where(Preset.name == item["name"])).first()
        if existing is not None:
            continue
        config = dict(item["config"])
        config.setdefault("retry_policy", default_retry_policy())
        db.add(Preset(name=item["name"], kind=item["kind"], config=config, is_seed=True))
        created += 1
    if created:
        db.commit()
    return created


def list_all(db: Session) -> list[Preset]:
    return list(db.exec(select(Preset).order_by(col(Preset.name))).all())


def get(db: Session, preset_id: int) -> Preset | None:
    return db.get(Preset, preset_id)


def config_for_repo(repo: Repository) -> dict[str, Any]:
    return {
        "kind": repo.kind,
        "tool": repo.tool,
        "method": repo.method,
        "upstream_host": repo.upstream_host,
        "upstream_root": repo.upstream_root,
        "suites": repo.suites,
        "sections": repo.sections,
        "architectures": repo.architectures,
        "include_source": repo.include_source,
        "diff_mode": repo.diff_mode,
        "cleanup_mode": repo.cleanup_mode,
        "verify_signatures": repo.verify_signatures,
        "rsync_extra": repo.rsync_extra,
        "include_patterns": repo.include_patterns,
        "exclude_patterns": repo.exclude_patterns,
        "extra_args": repo.extra_args,
        "extra_files": repo.extra_files,
        "bwlimit_kbps": repo.bwlimit_kbps,
        "retry_policy": repo.retry_policy,
        "export_mode": repo.export_mode,
        "export_format": repo.export_format,
        "export_split_mb": repo.export_split_mb,
        "chain_export": repo.chain_export,
        "enabled": repo.enabled,
    }


def save_from_repo(
    db: Session, repo: Repository, name: str, *, actor: str, ip: str = "", overwrite: bool = False
) -> Preset:
    clean = name.strip()[:128] or repo.name
    existing = db.exec(select(Preset).where(Preset.name == clean)).first()
    if existing is not None and not overwrite:
        raise ValueError(f"preset '{clean}' already exists")
    config = config_for_repo(repo)
    if existing is not None:
        existing.config = config
        existing.kind = repo.kind
        db.add(existing)
        preset = existing
        action = "preset.update"
    else:
        preset = Preset(name=clean, kind=repo.kind, config=config, is_seed=False)
        db.add(preset)
        action = "preset.create"
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action=action,
        target_type="preset",
        target_id=clean,
        detail={"from_repository": repo.slug},
        commit=False,
    )
    db.commit()
    db.refresh(preset)
    return preset


def delete(db: Session, preset: Preset, *, actor: str, ip: str = "") -> None:
    audit.record(
        db,
        actor=actor,
        ip=ip,
        action="preset.delete",
        target_type="preset",
        target_id=preset.name,
        detail={"was_seed": preset.is_seed},
        commit=False,
    )
    db.delete(preset)
    db.commit()
