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
