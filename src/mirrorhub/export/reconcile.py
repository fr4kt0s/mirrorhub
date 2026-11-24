"""Restore export history from the metadata copies on the state volume.

The copies under ``<state>/export-meta/<slug>/<export-id>/`` are the authority for
which exports were published (their ``.published`` marker). The ``export`` table is
rebuilt from them when rows are missing, for example after the database was lost and
the repositories were restored from a backup or a YAML import.

    python -m mirrorhub.export.reconcile [--slug SLUG] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.export import metadata_store
from mirrorhub.export.manifest import ExportError
from mirrorhub.models import Export, Repository
from mirrorhub.services import audit
from mirrorhub.settings import Settings, get_settings

log = logging.getLogger(__name__)


def _created_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _row_from_copy(settings: Settings, repo: Repository, export_id: str) -> Export:
    stored = metadata_store.directory(settings, repo.slug, export_id)
    try:
        metadata: dict[str, Any] = json.loads((stored / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"{export_id}: manifest.json is unreadable: {exc}") from exc
    if metadata.get("export_id") != export_id or metadata.get("repo") != repo.slug:
        raise ExportError(f"{export_id}: manifest.json does not describe this export")
    counts = metadata.get("counts") or {}
    sizes = metadata.get("sizes") or {}
    row = Export(
        export_id=export_id,
        repo_id=repo.id or 0,
        mode=str(metadata.get("mode") or "incr"),
        base_export_id=metadata.get("base_export_id") or None,
        path=str(settings.export_dir / repo.slug / export_id),
        status="published",
        file_count=int(counts.get("payload") or 0),
        deleted_count=int(counts.get("deleted") or 0),
        total_bytes=int(sizes.get("archive_bytes") or 0),
        signed=(stored / "MANIFEST.sha256.sig").is_file(),
    )
    created = _created_at(metadata.get("created_at"))
    if created is not None:
        row.created_at = created
    return row


def reconcile(
    db: Session,
    settings: Settings,
    *,
    slug: str | None = None,
    dry_run: bool = False,
    actor: str = "system",
) -> list[str]:
    """Mark old copies and create missing export rows. Returns "slug/export_id" created.

    Copies are marked when the database or the drop folder (.done) shows the export was
    published. Rows are only created, never changed: an existing row with another status
    is left to the worker's publication recovery. Repositories are matched by slug; a
    copy of a repository that no longer exists is skipped.
    """
    repositories = {repo.slug: repo for repo in db.exec(select(Repository)).all()}
    names = [slug] if slug is not None else metadata_store.slugs(settings)

    created: list[str] = []
    marked: list[str] = []
    for name in names:
        repo = repositories.get(name)
        if repo is None or repo.id is None:
            continue
        rows = db.exec(select(Export).where(Export.repo_id == repo.id)).all()
        known = {row.export_id for row in rows}
        published = {row.export_id for row in rows if row.status == "published"}

        if not dry_run:
            marked.extend(
                f"{name}/{export_id}"
                for export_id in metadata_store.backfill_markers(settings, name, published)
            )
        for copy in metadata_store.published_copies(settings, name):
            if copy.export_id in known:
                continue
            try:
                row = _row_from_copy(settings, repo, copy.export_id)
            except (ExportError, ValueError, TypeError) as exc:
                log.warning("cannot restore export row: %s", exc)
                continue
            if dry_run:
                created.append(f"{name}/{copy.export_id}")
                continue
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # created concurrently by another process
                continue
            created.append(f"{name}/{copy.export_id}")

    if (created or marked) and not dry_run:
        audit.record(
            db,
            actor=actor,
            action="exports.reconcile",
            target_type="export",
            target_id=slug or "*",
            detail={"rows_created": created, "copies_marked": marked},
        )
    return created


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mirrorhub.export.reconcile",
        description="Restore export history from the metadata copies on the state volume.",
    )
    parser.add_argument("--slug", help="only this repository")
    parser.add_argument("--dry-run", action="store_true", help="list rows, change nothing")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    with Session(get_engine()) as db:
        created = reconcile(
            db, settings, slug=arguments.slug, dry_run=arguments.dry_run, actor="cli"
        )
    verb = "would create" if arguments.dry_run else "created"
    for name in created:
        print(f"{verb} {name}")
    print(f"{len(created)} export rows {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
