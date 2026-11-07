"""Queue export jobs and reconcile completed filesystem publications."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, col, select

from mirrorhub.db import get_engine, immediate_session
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import audit
from mirrorhub.settings import Settings
from mirrorhub.verify import verify
import re
import shutil
from dataclasses import dataclass, field
from mirrorhub.services import appsettings
from mirrorhub.models import Export, Job, Repository, Setting
ACTIVE = ("queued", "running", "cancelling")


def enqueue(
    repo_id: int,
    *,
    mode: str | None,
    actor: str,
    ip: str = "",
) -> int:
    if mode is not None and mode not in ("full", "incr"):
        raise ExportError("export mode must be full or incr")

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
            return existing.id or 0

        job = Job(
            repo_id=repo_id,
            type="export",
            tool="export",
            trigger="manual",
            params={"mode": mode or repo.export_mode},
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
            detail={"repo": repo.slug, "mode": mode or repo.export_mode},
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
    return list(db.exec(select(Export).order_by(col(Export.id).desc()).limit(limit)).all())


def publication_directory(settings: Settings, slug: str, export_id: str) -> Path:
    path = settings.export_dir / slug / export_id
    if path.resolve().parent.parent != settings.export_dir.resolve():
        raise ExportError("export directory escapes the drop folder")
    return path
RETENTION_KEY = "export_keep_chains"
_EXPORT_ID = re.compile(r"\d{8}T\d{6}Z-(?:full|incr)")


def keep_chains(db: Session) -> int:
    try:
        return max(int(appsettings.get(db, RETENTION_KEY, 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class RetentionPlan:
    keep: int
    exports: list[Export] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
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
    rows = db.exec(
        select(Export).where(col(Export.status).in_(("failed", "building")))
    ).all()
    return [row for row in rows if row.status == "failed" or row.job_id not in active]


def delete_failed(
    db: Session, settings: Settings, *, actor: str, ip: str = ""
) -> list[str]:
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