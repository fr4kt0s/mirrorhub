"""Queue export jobs and reconcile completed filesystem publications."""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from mirrorhub.db import get_engine, immediate_session
from mirrorhub.export import manifest, metadata_store, reconcile, repair
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import appsettings, audit
from mirrorhub.settings import Settings, get_settings
from mirrorhub.verify import verify

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running", "cancelling")

SYNC_INVENTORY_RE = re.compile(r"^sync-\d+-attempt-\d+\.json$")
SYNC_INVENTORY_MAX_AGE_S = 7 * 86400


def _sync_inventory(settings: Settings, value: object) -> Path | None:
    """Return the path if value names a sync inventory inside the manifest directory."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not SYNC_INVENTORY_RE.fullmatch(path.name):
        return None
    if path.parent.resolve() != settings.manifest_dir.resolve():
        return None
    return path


def discard_sync_inventory(settings: Settings, value: object) -> bool:
    """Delete the sync inventory a chained export consumed. Returns True if a file was removed."""
    path = _sync_inventory(settings, value)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def purge_sync_inventories(
    db: Session,
    settings: Settings,
    *,
    max_age_s: int = SYNC_INVENTORY_MAX_AGE_S,
    now: float | None = None,
) -> int:
    """Delete sync inventories older than max_age_s that no queued or running export needs.

    Leftovers come from chained exports that failed permanently or were cancelled, and
    from versions that wrote an inventory after every sync.
    """
    if not settings.manifest_dir.is_dir():
        return 0
    in_use = set()
    for job in db.exec(
        select(Job).where(col(Job.type) == "export", col(Job.status).in_(ACTIVE))
    ).all():
        path = _sync_inventory(settings, (job.params or {}).get("sync_snapshot"))
        if path is not None:
            in_use.add(path.name)

    cutoff = (now if now is not None else time.time()) - max_age_s
    removed = 0
    for path in settings.manifest_dir.iterdir():
        if not SYNC_INVENTORY_RE.fullmatch(path.name) or path.name in in_use:
            continue
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def check_base(settings: Settings, slug: str, base_export_id: str) -> None:
    """Raise unless base_export_id is a published export with a usable metadata copy."""
    if not metadata_store.EXPORT_ID_RE.fullmatch(base_export_id):
        raise ExportError("invalid base export id")
    marked = {copy.export_id for copy in metadata_store.published_copies(settings, slug)}
    if base_export_id not in marked:
        raise ExportError(
            f"no published metadata copy of {base_export_id} is kept; "
            "run a full export and import it with --rebase"
        )
    metadata_store.load_base(settings, slug, base_export_id)


@dataclass(frozen=True)
class RepairPlan:
    """What a repair export of an uploaded request will do (shown before it starts)."""

    request: repair.Request
    published_after: list[str]  # exports after the base; the receiver skips them
    changes_since: int | None  # changed + deleted paths base -> newest published export
    newest: str | None


def plan_repair(settings: Settings, slug: str, request: repair.Request) -> RepairPlan:
    """Check a request against the kept metadata copies (ExportError if it cannot work)."""
    check_base(settings, slug, request.base_export_id)
    base = metadata_store.load_base(settings, slug, request.base_export_id)
    repair.check_against_base(request, base)
    newest = metadata_store.latest_published(settings, slug)
    after = sorted(
        copy.export_id
        for copy in metadata_store.published_copies(settings, slug)
        if copy.export_id > request.base_export_id
    )
    changes: int | None = None
    if newest is not None and newest != request.base_export_id:
        try:
            changed, deleted = manifest.diff(metadata_store.load_base(settings, slug, newest), base)
            changes = len(changed) + len(deleted)
        except ExportError:
            changes = None
    elif newest == request.base_export_id:
        changes = 0
    return RepairPlan(request, after, changes, newest)


def enqueue(
    repo_id: int,
    *,
    mode: str | None,
    actor: str,
    ip: str = "",
    base_export_id: str | None = None,
    repair_request: str | None = None,
    keep_extra: bool = False,
) -> int:
    """Queue an export. With base_export_id: an incremental export since that export.

    Without a base, an already queued or running export of the repository is returned
    instead of queueing a second one; with a base that is refused, because the running
    export would not use it.

    With repair_request (the SHA-256 of a kept, uploaded repair request): a repair
    export since the request's base (#43); keep_extra keeps the receiver's extra paths.
    """
    if mode is not None and mode not in ("full", "incr"):
        raise ExportError("export mode must be full or incr")
    if base_export_id is not None and mode not in (None, "incr"):
        raise ExportError("an export since an earlier export is always incremental")
    request: repair.Request | None = None

    if base_export_id is not None or repair_request is not None:
        # Outside the write transaction: checking the copy hashes files.json.
        with Session(get_engine()) as db:
            found = db.get(Repository, repo_id)
            slug = found.slug if found is not None else None
        if slug is None:
            raise ExportError("repository not found")
        if repair_request is not None:
            if mode not in (None, "incr"):
                raise ExportError("a repair export is always incremental")
            request = repair.load(get_settings().manifest_dir, slug, repair_request)
            if base_export_id not in (None, request.base_export_id):
                raise ExportError("a repair export is based on the request's export")
            base_export_id = request.base_export_id
            plan_repair(get_settings(), slug, request)
        else:
            assert base_export_id is not None
            check_base(get_settings(), slug, base_export_id)

    with immediate_session() as db:
        repo = db.get(Repository, repo_id)
        if repo is None:
            raise ExportError("repository not found")

        existing = db.exec(
            select(Job)
            .where(
                Job.repo_id == repo_id,
                Job.type == "export",
                col(Job.status).in_(ACTIVE),
            )
            .order_by(col(Job.id).desc())
        ).first()
        if existing is not None:
            if base_export_id is not None:
                raise ExportError(
                    f"export job {existing.id} of this repository is already queued or running"
                )
            return existing.id or 0

        params: dict[str, Any] = {"mode": mode or repo.export_mode}
        detail: dict[str, Any] = {"repo": repo.slug, "mode": mode or repo.export_mode}
        if base_export_id is not None:
            params = {"mode": "incr", "base": base_export_id}
            detail = {"repo": repo.slug, "mode": "incr", "base_export_id": base_export_id}
        if request is not None:
            params |= {"repair": request.sha256, "keep_extra": keep_extra}
            detail |= {
                "repair_request": request.sha256,
                "resend": len(request.resend),
                "delete": 0 if keep_extra else len(request.delete),
                "keep_extra": keep_extra,
            }

        job = Job(
            repo_id=repo_id,
            type="export",
            tool="export",
            trigger="manual",
            params=params,
            max_attempts=int(repo.retry_policy.get("max_attempts", 1)),
            requested_by=actor,
        )
        db.add(job)
        db.flush()
        audit.record(
            db,
            actor=actor,
            ip=ip,
            action="export.enqueue",
            target_type="job",
            target_id=str(job.id),
            detail=detail,
            commit=False,
        )
        return job.id or 0


def published_for_job(job_id: int, settings: Settings) -> str | None:
    """Recover publication completed before its database transaction committed."""
    with Session(get_engine()) as db:
        rows = db.exec(
            select(Export).where(Export.job_id == job_id).order_by(col(Export.id).desc())
        ).all()
        candidates = [(row.id, row.repo_id, row.export_id) for row in rows]

    for row_id, repo_id, export_id in candidates:
        with Session(get_engine()) as db:
            repo = db.get(Repository, repo_id)
            if repo is None:
                raise ExportError("export repository is missing")
            slug = repo.slug

        directory = settings.export_dir / slug / export_id
        if not (directory / ".done").is_file():
            continue

        metadata = verify(directory)
        if metadata["export_id"] != export_id or metadata["repo"] != slug:
            raise ExportError("published export identity does not match database")
        if metadata_store.directory(settings, slug, export_id).is_dir():
            metadata_store.mark_published(
                settings, slug, export_id, metadata=metadata, job_id=job_id
            )

        with immediate_session() as db:
            row = db.get(Export, row_id)
            if row is None:
                raise ExportError("export record is missing")
            was_published = row.status == "published"
            row.status = "published"
            row.path = str(directory)
            row.file_count = metadata["counts"]["payload"]
            row.deleted_count = metadata["counts"]["deleted"]
            row.total_bytes = metadata["sizes"]["archive_bytes"]
            row.signed = (directory / "MANIFEST.sha256.sig").is_file()
            db.add(row)
            if not was_published:
                audit.record(
                    db,
                    actor="worker",
                    action="export.recovered",
                    target_type="export",
                    target_id=export_id,
                    detail={"job_id": job_id},
                    commit=False,
                )
        return export_id

    return None


def list_exports(db: Session, limit: int = 200) -> list[Export]:
    """Return the newest export records, newest first."""
    return list(db.exec(select(Export).order_by(col(Export.id).desc()).limit(limit)).all())


def superseded(rows: list[Export]) -> set[int]:
    """Row ids of published exports a later export skipped by building on an older base.

    Example: E6 with base E3 supersedes E4 and E5. Export ids sort by time.
    """
    published: dict[int, list[Export]] = {}
    for row in rows:
        if row.status == "published":
            published.setdefault(row.repo_id, []).append(row)
    skipped: set[int] = set()
    for per_repo in published.values():
        for later in per_repo:
            base = later.base_export_id
            if later.mode != "incr" or not base:
                continue
            for row in per_repo:
                if base < row.export_id < later.export_id and row.id is not None:
                    skipped.add(row.id)
    return skipped


def base_choices(settings: Settings, slug: str) -> list[metadata_store.PublishedCopy]:
    """Kept, published metadata copies of a repository, newest first."""
    try:
        return list(reversed(metadata_store.published_copies(settings, slug)))
    except ExportError:
        return []


def publication_directory(settings: Settings, slug: str, export_id: str) -> Path:
    """Return an export's directory in the drop folder (ExportError if it escapes it)."""
    path = settings.export_dir / slug / export_id
    if path.resolve().parent.parent != settings.export_dir.resolve():
        raise ExportError("export directory escapes the drop folder")
    return path


RETENTION_KEY = "export_keep_chains"
_EXPORT_ID = re.compile(r"\d{8}T\d{6}Z-(?:full|incr)")


def keep_chains(db: Session) -> int:
    """Return the "chains to keep" setting (0: automatic retention off)."""
    try:
        return max(int(appsettings.get(db, RETENTION_KEY, 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class RetentionPlan:
    """Published exports that retention would remove, for the preview and the sweep."""

    keep: int
    exports: list[Export] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Archive bytes of all exports in the plan."""
        return sum(row.total_bytes for row in self.exports)


def retention_plan(db: Session, keep: int) -> RetentionPlan:
    """List published exports in chains older than the newest `keep` per repository."""
    plan = RetentionPlan(keep=keep)
    if keep < 1:
        return plan

    rows = db.exec(
        select(Export)
        .where(Export.status == "published")
        .order_by(col(Export.repo_id), col(Export.id))
    ).all()

    chains: dict[int, list[list[Export]]] = {}
    for row in rows:
        per_repo = chains.setdefault(row.repo_id, [])
        if row.mode == "full" or not per_repo:
            per_repo.append([])
        per_repo[-1].append(row)

    for per_repo in chains.values():
        for chain in per_repo[:-keep]:
            plan.exports.extend(chain)
    return plan


def apply_retention(
    db: Session, settings: Settings, *, actor: str, ip: str = ""
) -> tuple[list[str], int]:
    """Delete old export chains from disk and database. Returns names and freed bytes."""
    keep = keep_chains(db)
    plan = retention_plan(db, keep)
    slugs = {repo.id: repo.slug for repo in db.exec(select(Repository)).all()}

    removed: list[str] = []
    freed = 0
    try:
        for row in plan.exports:
            slug = slugs.get(row.repo_id)
            if slug is None or not _EXPORT_ID.fullmatch(row.export_id):
                continue
            directory = publication_directory(settings, slug, row.export_id)
            if directory.is_symlink():
                continue
            name = f"{slug}/{row.export_id}"
            size = row.total_bytes
            if directory.is_dir():
                (directory / ".done").unlink(missing_ok=True)
                shutil.rmtree(directory)
            metadata_store.remove(settings, slug, row.export_id)
            db.delete(row)
            db.commit()
            removed.append(name)
            freed += size
    finally:
        if removed:
            audit.record(
                db,
                actor=actor,
                ip=ip,
                action="exports.retention",
                target_type="export",
                target_id="*",
                detail={"keep_chains": keep, "removed": removed, "freed_bytes": freed},
                commit=False,
            )
            db.commit()
    return removed, freed


def failed_exports(db: Session) -> list[Export]:
    """Failed exports and stale 'building' rows without an active job."""
    active = set(db.exec(select(Job.id).where(col(Job.status).in_(ACTIVE))).all())
    rows = db.exec(select(Export).where(col(Export.status).in_(("failed", "building")))).all()
    return [row for row in rows if row.status == "failed" or row.job_id not in active]


def delete_failed(db: Session, settings: Settings, *, actor: str, ip: str = "") -> list[str]:
    """Remove failed/aborted export rows and their unfinished directories."""
    slugs = {repo.id: repo.slug for repo in db.exec(select(Repository)).all()}
    removed: list[str] = []
    try:
        for row in failed_exports(db):
            slug = slugs.get(row.repo_id)
            if slug is None or not _EXPORT_ID.fullmatch(row.export_id):
                continue
            directory = publication_directory(settings, slug, row.export_id)
            temporary = directory.with_name(f"{row.export_id}.tmp")
            if directory.is_symlink() or temporary.is_symlink():
                continue
            if (directory / ".done").is_file():
                continue  # complete on disk; recovery may still publish it
            if temporary.is_dir():
                shutil.rmtree(temporary)
            if directory.is_dir():
                shutil.rmtree(directory)
            name = f"{slug}/{row.export_id}"
            db.delete(row)
            db.commit()
            removed.append(name)
    finally:
        if removed:
            audit.record(
                db,
                actor=actor,
                ip=ip,
                action="exports.cleanup_failed",
                target_type="export",
                target_id="*",
                detail={"removed": removed},
                commit=False,
            )
            db.commit()
    return removed


# Files an export directory may still hold after the pickup tool took the archive.
_LEFTOVER_FILES = frozenset({*metadata_store.META_FILES, ".done"})
PICKED_UP_GRACE_S = 24 * 3600


def drop_folder_state(settings: Settings, slug: str, export_id: str) -> str:
    """Where a published export is now: 'present', 'partial' or 'picked up'."""
    try:
        directory = publication_directory(settings, slug, export_id)
    except ExportError:
        return "picked up"
    if not directory.is_dir():
        return "picked up"
    if (directory / ".done").is_file() and (directory / "manifest.json").is_file():
        return "present"
    return "partial"


def _latest_published(db: Session) -> dict[str, str]:
    slugs = {repo.id: repo.slug for repo in db.exec(select(Repository)).all()}
    latest: dict[str, str] = {}
    rows = db.exec(
        select(Export).where(Export.status == "published").order_by(col(Export.id))
    ).all()
    for row in rows:
        slug = slugs.get(row.repo_id)
        if slug is not None:
            latest[slug] = row.export_id
    return latest


def _only_leftovers(directory: Path) -> bool:
    """Return True if the directory holds no archive part, only small metadata files."""
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file() or entry.name not in _LEFTOVER_FILES:
            return False
    return True


def clean_drop_folder(db: Session, settings: Settings, *, now: float | None = None) -> list[str]:
    """Remove export directories the pickup tool has emptied.

    Removed: empty export directories, and directories that hold only metadata files
    (no archive part) and have not changed for PICKED_UP_GRACE_S, so a tool that is
    still moving files is not disturbed. Empty repository directories are removed
    when no export of that repository is running. In-progress `.tmp` directories and
    anything unexpected are left alone. Metadata copies on the state volume are kept.
    """
    root = settings.export_dir
    if not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - PICKED_UP_GRACE_S
    busy = {
        row.repo_id
        for row in db.exec(
            select(Job).where(col(Job.type) == "export", col(Job.status).in_(ACTIVE))
        ).all()
    }
    repo_ids = {repo.slug: repo.id for repo in db.exec(select(Repository)).all()}

    removed: list[str] = []
    for repo_dir in sorted(root.iterdir()):
        if repo_dir.is_symlink() or not repo_dir.is_dir():
            continue
        if repo_dir.name.startswith("_"):
            continue  # reserved, e.g. _events from the file notification channel
        for directory in sorted(repo_dir.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            if not _EXPORT_ID.fullmatch(directory.name):
                continue  # includes <id>.tmp directories of running exports
            try:
                if not any(directory.iterdir()):
                    directory.rmdir()
                elif _only_leftovers(directory) and directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory)
                else:
                    continue
            except OSError:
                continue
            removed.append(f"{repo_dir.name}/{directory.name}")

        if repo_ids.get(repo_dir.name) in busy:
            continue
        try:
            if not any(repo_dir.iterdir()):
                repo_dir.rmdir()
                removed.append(f"{repo_dir.name}/")
        except OSError:
            continue
    return removed


def protected_bases(db: Session, settings: Settings) -> dict[str, set[str]]:
    """Per slug, the newest published export by database and by markers."""
    protected: dict[str, set[str]] = {}
    for slug, export_id in _latest_published(db).items():
        protected.setdefault(slug, set()).add(export_id)
    for slug in metadata_store.slugs(settings):
        latest = metadata_store.latest_published(settings, slug)
        if latest is not None:
            protected.setdefault(slug, set()).add(latest)
    return protected


def housekeeping(db: Session, settings: Settings) -> dict[str, int]:
    """Periodic export cleanup run by the scheduler."""
    restored = 0
    try:
        restored = len(reconcile.reconcile(db, settings, actor="scheduler"))
    except (ExportError, ValueError):
        log.exception("export history reconcile failed")
    pruned = metadata_store.prune(
        settings, protected_bases(db, settings), keep=settings.export_meta_keep
    )
    cleaned = clean_drop_folder(db, settings)
    if cleaned:
        audit.record(
            db,
            actor="scheduler",
            action="exports.drop_folder_cleanup",
            target_type="export",
            target_id="*",
            detail={"removed": cleaned},
        )
    return {
        "metadata_pruned": pruned,
        "drop_folder_removed": len(cleaned),
        "export_rows_restored": restored,
    }
