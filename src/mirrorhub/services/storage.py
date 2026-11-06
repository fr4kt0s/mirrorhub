"""Disk usage: mount statistics from statvfs, per-repository sizing cached in the DB.

Sizing walks the tree, which is slow on large mirrors, so it always runs in a single
background thread and results are cached with a timestamp.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Repository, StorageCache, utcnow
from mirrorhub.settings import Settings, get_settings

log = logging.getLogger("mirrorhub.storage")

SKIP_DIRS = frozenset({"partial", ".temp", ".tmp", "tmp", "lost+found"})


@dataclass(frozen=True)
class MountUsage:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float


def mount_usage(path: Path | None = None) -> MountUsage:
    settings = get_settings()
    target = path or settings.data_dir
    try:
        target.mkdir(parents=True, exist_ok=True)
        stats = os.statvfs(target)
    except OSError as exc:
        log.warning("statvfs failed", extra={"path": str(target), "error": str(exc)})
        return MountUsage(str(target), 0, 0, 0, 0.0)
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    used = max(total - free, 0)
    percent = (used / total * 100.0) if total else 0.0
    return MountUsage(str(target), total, used, free, round(percent, 1))


def repo_path(slug: str, settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / slug


def _bucket(relative: str, name: str, architectures: list[str]) -> str:
    haystack = f"{relative}/{name}"
    for arch in architectures:
        if (
            f"/binary-{arch}/" in haystack
            or f"/os/{arch}/" in haystack
            or f"_{arch}." in haystack
            or f"Contents-{arch}" in haystack
        ):
            return arch
    if "/dists/" in haystack and name.startswith(("Release", "InRelease")):
        return "all"
    if name.endswith(("Release", "Release.gpg", "InRelease")) or "Packages" in name:
        return "all"
    return "other"


def scan_repo(path: Path, architectures: list[str]) -> tuple[int, dict[str, int]]:
    """Return (total bytes, bytes per architecture bucket)."""
    per_arch: dict[str, int] = dict.fromkeys(architectures, 0)
    per_arch.setdefault("all", 0)
    per_arch.setdefault("other", 0)
    total = 0
    if not path.is_dir():
        return 0, per_arch

    stack: list[tuple[str, str]] = [(str(path), "")]
    while stack:
        current, relative = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    stack.append(
                        (entry.path, f"{relative}/{entry.name}" if relative else entry.name)
                    )
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    total += size
                    bucket = _bucket(relative, entry.name, architectures)
                    per_arch[bucket] = per_arch.get(bucket, 0) + size
            except OSError:
                continue
    return total, per_arch


def refresh_all(settings: Settings | None = None) -> int:
    """Recompute sizes for every repository. Blocking; call from a background thread."""
    settings = settings or get_settings()
    updated = 0
    with Session(get_engine()) as db:
        repositories = list(db.exec(select(Repository)).all())
        for repo in repositories:
            total, per_arch = scan_repo(repo_path(repo.slug, settings), list(repo.architectures))
            row = db.get(StorageCache, repo.id or 0)
            if row is None:
                row = StorageCache(repo_id=repo.id or 0)
            row.bytes = total
            row.per_arch_bytes = per_arch
            row.computed_at = utcnow()
            db.add(row)
            updated += 1
        db.commit()
    log.info("storage cache refreshed", extra={"repositories": updated})
    return updated


_lock = threading.Lock()
_running = False


def start_refresh(settings: Settings | None = None) -> bool:
    """Start one background refresh. Returns False when a refresh is already running."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def _work() -> None:
        global _running
        try:
            refresh_all(settings)
        except Exception:
            log.exception("storage refresh failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_work, name="storage-refresh", daemon=True).start()
    return True


def is_refreshing() -> bool:
    return _running


def cached(db: Session) -> dict[int, StorageCache]:
    return {row.repo_id: row for row in db.exec(select(StorageCache)).all()}
