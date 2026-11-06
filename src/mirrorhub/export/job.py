"""Build and atomically publish repository exports."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from mirrorhub import __version__
from mirrorhub.db import get_engine
from mirrorhub.export import manifest, pack
from mirrorhub.export.manifest import ExportError, Inventory, Pulse
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import audit
from mirrorhub.settings import Settings

FORMAT_VERSION = 1
CHECKSUM_EXCLUSIONS = frozenset({"MANIFEST.sha256", "MANIFEST.sha256.sig", ".done"})


def sync_snapshot_path(
    settings: Settings,
    job_id: int,
    attempt: int,
) -> Path:
    return settings.manifest_dir / f"sync-{job_id}-attempt-{attempt}.json"


def capture_sync(
    settings: Settings,
    repo: Repository,
    job: Job,
    pulse: Pulse,
) -> Path:
    path = sync_snapshot_path(settings, job.id or 0, job.attempt)
    if path.exists():
        raise ExportError("sync inventory already exists for this attempt")
    inventory = manifest.snapshot(settings.data_dir / repo.slug, pulse)
    manifest.save(path, inventory)
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


def _write_checksums(directory: Path, pulse: Pulse) -> None:
    """Collect payload paths before creating the checksum manifest."""
    payloads = sorted(path for path in directory.iterdir() if path.name not in CHECKSUM_EXCLUSIONS)
    for path in payloads:
        if path.is_symlink() or not path.is_file():
            raise ExportError(f"unsupported package entry: {path.name}")

    with (directory / "MANIFEST.sha256").open(
        "x",
        encoding="ascii",
    ) as handle:
        for path in payloads:
            digest = manifest.file_hash(path, pulse)
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
    with Session(get_engine()) as db:
        row = db.exec(
            select(Export)
            .where(
                Export.repo_id == repo_id,
                Export.status == "published",
            )
            .order_by(col(Export.id).desc())
        ).first()
        if row is None:
            return None, {}
        export_id = row.export_id

    directory = settings.export_dir / slug / export_id
    if not (directory / ".done").is_file():
        raise ExportError("last published export is missing its .done marker")

    from mirrorhub.verify import verify

    metadata = verify(directory)
    if metadata["repo"] != slug or metadata["export_id"] != export_id:
        raise ExportError("base export identity does not match repository")
    return export_id, manifest.load(directory / "files.json")


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

    pulse(0, "Building repository inventory")
    current = manifest.snapshot(root, pulse)

    expected_path = job.params.get("sync_snapshot")
    if expected_path:
        expected = Path(str(expected_path))
        if expected.resolve().parent != settings.manifest_dir.resolve():
            raise ExportError("sync inventory path is outside the manifest directory")
        if manifest.load(expected) != current:
            raise ExportError("repository changed after sync; run sync and export again")

    if requested == "full":
        base_id: str | None = None
        previous: Inventory = {}
    else:
        base_id, previous = _previous(repo.id or 0, settings, repo.slug)

    mode = requested if base_id else "full"
    changed, deleted = manifest.diff(current, previous)
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
        raw_archive = temporary / "data.tar"
        pack.archive(root, raw_archive, current, changed, pulse)

        payload = raw_archive
        if repo.export_format == "tar.zst":
            payload = temporary / "data.tar.zst"
            pack.compress(raw_archive, payload, pulse)
            raw_archive.unlink()

        parts = pack.split(
            payload,
            repo.export_split_mb * 1024 * 1024,
            pulse,
        )
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
                "python": platform.python_version(),
                "archive_writer": "Python tarfile",
            },
        }
        _write_json(temporary / "manifest.json", metadata)

        pulse(85, "Checking repository did not change during packing")
        if manifest.snapshot(root, pulse) != current:
            raise ExportError("repository changed during export; publication refused")

        _write_checksums(temporary, pulse)
        signed = _sign(temporary, settings, pulse)

        for path in temporary.iterdir():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(temporary)

        pulse(98, "Publishing export")
        os.rename(temporary, final)
        _fsync_directory(parent)

        with (final / ".done").open("x", encoding="ascii") as handle:
            handle.write(f"{export_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(final)

        with Session(get_engine()) as db:
            row = db.get(Export, row_id)
            if row is None:
                raise ExportError("export database record disappeared")
            row.status = "published"
            row.signed = signed
            row.file_count = len(changed)
            row.deleted_count = len(deleted)
            row.total_bytes = metadata["sizes"]["archive_bytes"]
            db.add(row)
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
                },
                commit=False,
            )
            db.commit()

        pulse(100, "Export published")
        return export_id
    except BaseException:
        _record_failure(row_id, export_id, repo.slug)
        raise
