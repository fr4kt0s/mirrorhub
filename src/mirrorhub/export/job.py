"""Build and atomically publish repository exports."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from mirrorhub import __version__, buildinfo
from mirrorhub.db import get_engine
from mirrorhub.export import manifest, metadata_store, pack, reconcile, repair
from mirrorhub.export.manifest import ExportError, Inventory, Pulse
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import audit
from mirrorhub.settings import Settings

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
CHECKSUM_EXCLUSIONS = frozenset({"MANIFEST.sha256", "MANIFEST.sha256.sig", ".done"})


def sync_snapshot_path(
    settings: Settings,
    job_id: int,
    attempt: int,
) -> Path:
    return settings.manifest_dir / f"sync-{job_id}-attempt-{attempt}.json"


def hash_cache_path(settings: Settings, slug: str) -> Path:
    """Per-repository SHA-256 cache on the local state volume (never on NFS)."""
    return settings.manifest_dir / f"hashcache-{slug}.json"


def capture_sync(
    settings: Settings,
    repo: Repository,
    job: Job,
    pulse: Pulse,
) -> Path:
    path = sync_snapshot_path(settings, job.id or 0, job.attempt)
    if path.exists():
        raise ExportError("sync inventory already exists for this attempt")
    cache_path = hash_cache_path(settings, repo.slug)
    cache = manifest.load_hash_cache(cache_path)
    inventory = manifest.snapshot(
        settings.data_dir / repo.slug, pulse, cache, threads=settings.inventory_threads
    )
    manifest.save(path, inventory)
    manifest.save_hash_cache(cache_path, cache)
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _same_metadata(left: Inventory, right: Inventory) -> bool:
    """Equal apart from file digests that are not known yet (empty)."""
    if left.keys() != right.keys():
        return False
    for path, entry in left.items():
        other = right[path]
        if {k: v for k, v in entry.items() if k != "sha256"} != {
            k: v for k, v in other.items() if k != "sha256"
        }:
            return False
        if entry.get("sha256") and other.get("sha256") and entry["sha256"] != other["sha256"]:
            return False
    return True


def _fill_digests(
    current: Inventory,
    digests: dict[str, str],
    stat_keys: manifest.StatKeys,
    cache: manifest.HashCache,
) -> None:
    """Put the digests computed while packing into the inventory and the hash cache."""
    for path, entry in current.items():
        if entry["type"] != "file":
            continue
        digest = digests.get(path)
        if digest is None:
            raise ExportError(f"file was not packed: {path}")
        entry["sha256"] = digest
        key, cacheable = stat_keys[path]
        if cacheable:
            cache[path] = [*key, digest]


def _write_checksums(directory: Path, pulse: Pulse, known: dict[str, str] | None = None) -> None:
    """Collect payload paths before creating the checksum manifest.

    ``known`` holds digests computed while the archive parts were written; those
    files are not read again.
    """
    payloads = sorted(path for path in directory.iterdir() if path.name not in CHECKSUM_EXCLUSIONS)
    for path in payloads:
        if path.is_symlink() or not path.is_file():
            raise ExportError(f"unsupported package entry: {path.name}")

    with (directory / "MANIFEST.sha256").open(
        "x",
        encoding="ascii",
    ) as handle:
        for path in payloads:
            digest = (known or {}).get(path.name) or manifest.file_hash(path, pulse)
            handle.write(f"{digest}  {path.name}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sign(directory: Path, settings: Settings, pulse: Pulse) -> bool:
    fingerprint = os.environ.get("MIRRORHUB_SIGNING_KEY", "").strip()
    if not fingerprint:
        return False

    valid_length = len(fingerprint) in (40, 64)
    valid_characters = all(character in "0123456789abcdefABCDEF" for character in fingerprint)
    if not valid_length or not valid_characters:
        raise ExportError("MIRRORHUB_SIGNING_KEY must be a full key fingerprint")

    executable = shutil.which("gpg")
    if executable is None:
        raise ExportError("gpg is not installed")

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    pulse(92, "Signing export checksum manifest")

    with tempfile.TemporaryDirectory(dir=settings.tmp_dir) as temporary:
        home = Path(temporary) / "gnupg"
        shutil.copytree(
            settings.gnupg_dir,
            home,
            ignore=shutil.ignore_patterns("S.gpg-agent*", "*.lock", ".#*"),
        )
        os.chmod(home, 0o700)

        arguments = [
            executable,
            "--no-options",
            "--homedir",
            str(home),
            "--batch",
            "--no-tty",
            "--local-user",
            fingerprint,
            "--output",
            str(directory / "MANIFEST.sha256.sig"),
            "--detach-sign",
            str(directory / "MANIFEST.sha256"),
        ]
        try:
            result = subprocess.run(  # noqa: S603
                arguments,
                capture_output=True,
                timeout=120,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExportError("export signing timed out") from exc

        if result.returncode != 0:
            raise ExportError("export signing failed; check signing-key availability")

    return True


def _previous(
    repo_id: int,
    settings: Settings,
    slug: str,
) -> tuple[str | None, Inventory]:
    """Base of the next incremental export: the newest published export.

    The marked metadata copies on the state volume decide; the database is checked
    against them. Missing rows are restored from the copies, so a lost database does
    not force a full export. If the database knows a newer published export than the
    copies and the drop folder no longer holds it, the export stops instead of falling
    back to an older base the receiver may already have passed.
    """
    with Session(get_engine()) as db:
        reconcile.reconcile(db, settings, slug=slug, actor="worker")
        row = db.exec(
            select(Export)
            .where(
                Export.repo_id == repo_id,
                Export.status == "published",
            )
            .order_by(col(Export.id).desc())
        ).first()
        db_latest = row.export_id if row is not None else None

    marked = {copy.export_id for copy in metadata_store.published_copies(settings, slug)}
    if db_latest is not None and db_latest not in marked:
        # Copy missing or unmarked: load_base falls back to the drop folder and stores a
        # copy; without either it asks for a full export.
        try:
            metadata_store.load_base(settings, slug, db_latest)
        except ExportError as exc:
            raise ExportError(
                f"{exc}; or start an incremental export since an earlier export "
                "the receiver has applied"
            ) from exc
        metadata_store.mark_published(settings, slug, db_latest)

    export_id = metadata_store.latest_published(settings, slug)
    if export_id is None:
        return None, {}
    # The base comes from the metadata copy on the state volume, so the drop folder
    # may be emptied by the pickup tool. Archive parts are neither needed nor hashed.
    return export_id, metadata_store.load_base(settings, slug, export_id)


def _chosen_base(job: Job, settings: Settings, slug: str) -> str | None:
    """Base picked by the operator ("incremental since"), validated again at run time."""
    value = job.params.get("base")
    if value is None:
        return None
    if not isinstance(value, str) or not metadata_store.EXPORT_ID_RE.fullmatch(value):
        raise ExportError("invalid base export id")
    if job.params.get("sync_snapshot"):
        raise ExportError("a chained export cannot use a chosen base")
    marked = {copy.export_id for copy in metadata_store.published_copies(settings, slug)}
    if value not in marked:
        raise ExportError(
            f"base export {value} has no published metadata copy; "
            "run a full export and import it with --rebase"
        )
    return value


def _repair_request(
    job: Job, settings: Settings, slug: str, base_id: str | None
) -> repair.Request | None:
    """The kept repair request of a repair export job (#43), checked again."""
    value = job.params.get("repair")
    if value is None:
        return None
    request = repair.load(settings.manifest_dir, slug, str(value))
    if base_id is None or request.base_export_id != base_id:
        raise ExportError("a repair export must be based on the request's export")
    return request


def _record_failure(row_id: int | None, export_id: str, slug: str) -> None:
    with Session(get_engine()) as db:
        row = db.get(Export, row_id)
        if row is None or row.status == "published":
            return
        row.status = "failed"
        db.add(row)
        audit.record(
            db,
            actor="worker",
            action="export.failed",
            target_type="export",
            target_id=export_id,
            detail={"repo": slug},
            commit=False,
        )
        db.commit()


def execute(
    job: Job,
    repo: Repository,
    settings: Settings,
    pulse: Pulse,
) -> str:
    requested = str(job.params.get("mode") or repo.export_mode)
    if requested not in ("full", "incr"):
        raise ExportError("invalid export mode")
    if repo.export_format not in ("tar", "tar.zst"):
        raise ExportError("invalid export archive format")
    if repo.export_split_mb < 0:
        raise ExportError("export split size must not be negative")

    root = settings.data_dir / repo.slug
    if root.resolve().parent != settings.data_dir.resolve():
        raise ExportError("repository path escapes the data directory")

    # Incremental exports reuse cached hashes for files whose stat data is unchanged.
    # Full exports ignore the cache and hash every file, but while packing it: the
    # inventory is built from stat data only, so the tree is read once (not twice).
    cache_path = hash_cache_path(settings, repo.slug)
    full_request = requested == "full"
    cache = {} if full_request else manifest.load_hash_cache(cache_path)
    stat_keys: manifest.StatKeys = {}

    pulse(0, "Building repository inventory")
    current = manifest.snapshot(
        root,
        pulse,
        cache,
        hash_files=not full_request,
        threads=settings.inventory_threads,
        stat_keys=stat_keys,
    )
    if not full_request:
        manifest.save_hash_cache(cache_path, cache)

    expected_path = job.params.get("sync_snapshot")
    expected_inventory: Inventory | None = None
    if expected_path:
        expected = Path(str(expected_path))
        if expected.resolve().parent != settings.manifest_dir.resolve():
            raise ExportError("sync inventory path is outside the manifest directory")
        expected_inventory = manifest.load(expected)
        if not _same_metadata(expected_inventory, current):
            raise ExportError("repository changed after sync; run sync and export again")

    chosen = _chosen_base(job, settings, repo.slug)
    if chosen is not None:
        if requested != "incr":
            raise ExportError("an export since an earlier export is always incremental")
        base_id: str | None = chosen
        previous: Inventory = metadata_store.load_base(settings, repo.slug, chosen)
    elif requested == "full":
        base_id = None
        previous = {}
    else:
        base_id, previous = _previous(repo.id or 0, settings, repo.slug)

    mode = requested if base_id else "full"
    changed, deleted = manifest.diff(current, previous)
    request = _repair_request(job, settings, repo.slug, chosen)
    repaired: repair.Applied | None = None
    if request is not None:
        repair.check_against_base(request, previous)
        repaired = repair.apply(
            request,
            current,
            changed,
            deleted,
            keep_extra=bool(job.params.get("keep_extra")),
        )
        changed, deleted = repaired.changed, repaired.deleted
    created_at = datetime.now(UTC)
    export_id = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{mode}"

    parent = settings.export_dir / repo.slug
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve().parent != settings.export_dir.resolve():
        raise ExportError("export path escapes the drop folder")

    final = parent / export_id
    temporary = parent / f"{export_id}.tmp"
    if final.exists() or temporary.exists():
        raise ExportError("export timestamp collision; retry after the next second")
    temporary.mkdir(mode=0o750)

    with Session(get_engine()) as db:
        row = Export(
            export_id=export_id,
            repo_id=repo.id or 0,
            mode=mode,
            base_export_id=base_id,
            path=str(final),
            job_id=job.id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id

    try:
        part_digests, file_digests = pack.write_archive(
            root,
            temporary,
            current,
            changed,
            pulse,
            compress=repo.export_format == "tar.zst",
            part_bytes=repo.export_split_mb * 1024 * 1024,
        )
        parts = [path for path, _digest in part_digests]
        if full_request:
            _fill_digests(current, file_digests, stat_keys, cache)
            manifest.save_hash_cache(cache_path, cache)
            if expected_inventory is not None and expected_inventory != current:
                raise ExportError("repository changed after sync; run sync and export again")
        manifest.save(temporary / "files.json", current)
        (temporary / "deleted.txt").write_text(
            "".join(f"{path}\n" for path in deleted),
            encoding="utf-8",
        )

        metadata: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "export_id": export_id,
            "repo": repo.slug,
            "mode": mode,
            "base_export_id": base_id,
            "created_at": created_at.isoformat(),
            "archive_format": repo.export_format,
            "archive_parts": [path.name for path in parts],
            "payload_paths": changed,
            "counts": {
                "inventory": len(current),
                "payload": len(changed),
                "deleted": len(deleted),
            },
            "sizes": {
                "payload_bytes": sum(current[path]["size"] for path in changed),
                "archive_bytes": sum(path.stat().st_size for path in parts),
            },
            "tool_versions": {
                "mirrorhub": __version__,
                "mirrorhub_commit": buildinfo.commit(),
                "python": platform.python_version(),
                "archive_writer": "Python tarfile",
            },
        }
        if repaired is not None:
            metadata["repair"] = repaired.field
        _write_json(temporary / "manifest.json", metadata)

        pulse(85, "Checking repository did not change during packing")
        if manifest.snapshot(root, pulse, cache, threads=settings.inventory_threads) != current:
            raise ExportError("repository changed during export; publication refused")

        _write_checksums(temporary, pulse, {path.name: digest for path, digest in part_digests})
        signed = _sign(temporary, settings, pulse)

        for path in temporary.iterdir():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(temporary)

        # Before publishing: once .done exists, the pickup tool may move the export.
        pulse(97, "Storing export metadata")
        metadata_store.save(settings, repo.slug, export_id, temporary)

        pulse(98, "Publishing export")
        os.rename(temporary, final)
        _fsync_directory(parent)

        with (final / ".done").open("x", encoding="ascii") as handle:
            handle.write(f"{export_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(final)
        try:
            metadata_store.mark_published(
                settings, repo.slug, export_id, metadata=metadata, job_id=job.id
            )
        except (OSError, ExportError):
            # .done exists, so the export is published; reconcile marks the copy later.
            log.warning("could not mark metadata copy of %s as published", export_id)

        with Session(get_engine()) as db:
            published = db.get(Export, row_id)
            if published is None:
                raise ExportError("export database record disappeared")
            published.status = "published"
            published.signed = signed
            published.file_count = len(changed)
            published.deleted_count = len(deleted)
            published.total_bytes = metadata["sizes"]["archive_bytes"]
            db.add(published)
            audit.record(
                db,
                actor="worker",
                action="export.publish",
                target_type="export",
                target_id=export_id,
                detail={
                    "repo": repo.slug,
                    "mode": mode,
                    "base_export_id": base_id,
                    **({"repair": repaired.field} if repaired is not None else {}),
                },
                commit=False,
            )
            db.commit()
        if request is not None:
            repair.discard(settings.manifest_dir, repo.slug, request.sha256)

        pulse(100, "Export published")
        return export_id
    except BaseException:
        _record_failure(row_id, export_id, repo.slug)
        raise
