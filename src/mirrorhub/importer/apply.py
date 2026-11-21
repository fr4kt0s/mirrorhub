"""Plan, stage, apply and post-check an export against a local mirror tree.

Flow: verify -> signature -> lock -> chain check -> plan -> stream into staging ->
journal -> deletes -> per-file replace -> post-check -> state update -> cleanup.
A failure after the journal exists is rolled forward by running the same import again.
The export directory is only ever read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mirrorhub import buildinfo
from mirrorhub.importer import tree
from mirrorhub.importer.checks import (
    SKIP_DIRECTORIES,
    Export,
    check_signature,
    load_export,
    load_metadata,
    safe_relative,
)
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import (
    ChainError,
    ConfigError,
    ImporterError,
    IoFailure,
    PostCheckError,
)
from mirrorhub.importer.extract import extract_payload
from mirrorhub.importer.logs import log_event
from mirrorhub.importer.reader import open_archive
from mirrorhub.importer.state import RepoState, StateStore


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    rebase: bool = False
    full_check: bool = False
    discard_staging: bool = False
    # Quick comparison of the whole tree with files.json after each import.
    tree_check: bool = True


@dataclass
class Plan:
    adds: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    export_id: str
    status: str
    plan: Plan
    # Differences between the tree and files.json found after applying (paths the
    # export did not touch, e.g. deleted by hand). The export itself was applied.
    drift: list[str] = field(default_factory=list)


def _tree_paths(root: Path) -> set[str]:
    found: set[str] = set()
    if not root.is_dir():
        return found
    for directory, names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        links = [name for name in names if (base / name).is_symlink()]
        names[:] = [n for n in names if n not in SKIP_DIRECTORIES and n not in links]
        for name in [*filenames, *links]:
            found.add((base / name).relative_to(root).as_posix())
    return found


def build_plan(export: Export, target: Path, replace: bool) -> Plan:
    plan = Plan()
    for relative in sorted(export.payload):
        (plan.changes if os.path.lexists(target / relative) else plan.adds).append(relative)
    deletes = {path for path in export.deleted if os.path.lexists(target / path)}
    if replace:
        deletes |= _tree_paths(target) - set(export.inventory)
    plan.deletes = sorted(deletes)
    return plan


def check_chain(state: RepoState, export: Export, tree_has_entries: bool, rebase: bool) -> None:
    if export.mode == "full":
        if (state.last_export_id is not None or tree_has_entries) and not rebase:
            raise ChainError(
                "a full export over existing state or content requires --rebase "
                f"(last applied: {state.last_export_id})"
            )
        return
    if state.last_export_id is None:
        raise ChainError("incremental export but no export has been applied yet")
    if export.base_export_id != state.last_export_id:
        raise ChainError(
            f"base mismatch: export {export.export_id} expects base "
            f"{export.base_export_id}, last applied is {state.last_export_id}"
        )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check_entry(root: Path, relative: str, entry: dict[str, Any]) -> None:
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PostCheckError(f"missing after apply: {relative}") from exc
    if entry["type"] == "symlink":
        if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != entry["target"]:
            raise PostCheckError(f"symlink mismatch: {relative}")
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
            raise PostCheckError(f"file type or size mismatch: {relative}")
        if _file_digest(path) != entry["sha256"]:
            raise PostCheckError(f"sha256 mismatch: {relative}")
    if info.st_mtime_ns != entry["mtime_ns"]:
        raise PostCheckError(f"mtime mismatch: {relative}")


def post_check(export: Export, target: Path, journal_deletes: list[str], full: bool) -> None:
    """The resulting tree must match files.json (touched paths, or everything if full)."""
    for relative in journal_deletes:
        if os.path.lexists(target / relative):
            raise PostCheckError(f"deleted path still exists: {relative}")
    if not full:
        for relative in sorted(export.payload):
            _check_entry(target, relative, export.inventory[relative])
        return
    present = _tree_paths(target)
    extra = present - set(export.inventory)
    if extra:
        raise PostCheckError(f"{len(extra)} paths not in inventory, e.g. {sorted(extra)[0]}")
    for relative, entry in export.inventory.items():
        _check_entry(target, relative, entry)


def _parents(root: Path, relative: str, create: bool) -> bool:
    """Walk parent directories without following symlinks; False if one is absent."""
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                return False
            current.mkdir()
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise IoFailure(f"unsafe or conflicting parent in mirror tree: {current}")
    return True


def _prune(root: Path, relative: str) -> None:
    parent = (root / relative).parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_journal(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise IoFailure(f"unreadable journal {path}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("export_id"), str)
        or not all(
            isinstance(raw.get(key), list)
            and all(isinstance(item, str) and safe_relative(item) for item in raw[key])
            for key in ("deletes", "installs")
        )
    ):
        raise IoFailure(f"invalid journal {path}")
    return raw


def apply_journal(target: Path, files: Path, journal: dict[str, Any]) -> None:
    """Idempotent: deletes first, then per-file replace from staging."""
    for relative in journal["deletes"]:
        if not _parents(target, relative, create=False):
            continue
        path = target / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            raise IoFailure(f"cannot delete, path is a directory: {relative}")
        path.unlink()
        _prune(target, relative)
    for relative in journal["installs"]:
        source = files / relative
        if not os.path.lexists(source):
            continue
        _parents(target, relative, create=True)
        destination = target / relative
        if destination.is_dir() and not destination.is_symlink():
            raise IoFailure(f"cannot install over a directory: {relative}")
        os.replace(source, destination)


def _same_filesystem(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _apply_locked(
    export: Export, config: Config, options: Options, store: StateStore, state: RepoState
) -> Outcome:
    target = config.mirror_root / export.slug
    staging = config.staging_dir / export.slug
    journal_path = staging / "journal.json"
    files = staging / "files"
    replace = options.rebase and export.mode == "full"

    if config.staging_dir.resolve().is_relative_to(target.resolve()):
        raise ConfigError("staging_dir must not be inside the mirror tree of a repository")
    journal = _read_journal(journal_path)
    resuming = journal is not None and journal["export_id"] == export.export_id
    if journal is not None and not resuming and not options.discard_staging:
        raise IoFailure(
            f"staging holds an unfinished import of {journal['export_id']}; "
            "re-run that export or pass --discard-staging"
        )
    if not resuming:
        target_has_entries = bool(_tree_paths(target))
        check_chain(state, export, target_has_entries, options.rebase)

    target.mkdir(parents=True, exist_ok=True)
    config.staging_dir.mkdir(parents=True, exist_ok=True)
    if not _same_filesystem(config.staging_dir, target):
        raise ConfigError("staging_dir must be on the same filesystem as the mirror tree")

    plan = Plan()
    if resuming:
        assert journal is not None
        log_event(
            logging.WARNING,
            "resuming interrupted import",
            slug=export.slug,
            export_id=export.export_id,
        )
    else:
        plan = build_plan(export, target, replace)
        shutil.rmtree(staging, ignore_errors=True)
        files.mkdir(parents=True)
        try:
            with open_archive(export) as stream:
                extract_payload(export, stream, files)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        journal = {
            "export_id": export.export_id,
            "mode": export.mode,
            "deletes": plan.deletes,
            "installs": sorted(export.payload),
        }
        _write_json(journal_path, journal)
        log_event(
            logging.INFO,
            "staged",
            slug=export.slug,
            export_id=export.export_id,
            files=len(export.payload),
        )

    apply_journal(target, files, journal)
    try:
        post_check(export, target, journal["deletes"], options.full_check or export.mode == "full")
    except PostCheckError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    store.save_inventory(export.export_id, export.directory / "files.json")
    state.last_export_id = export.export_id
    state.history.append(
        {
            "export_id": export.export_id,
            "mode": export.mode,
            "base_export_id": export.base_export_id,
            "applied_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "counts": {"payload": len(export.payload), "deleted": len(journal["deletes"])},
            "rebase": replace,
        }
    )
    store.save(state)
    shutil.rmtree(staging, ignore_errors=True)
    log_event(logging.INFO, "applied", slug=export.slug, export_id=export.export_id)
    if resuming:
        plan = Plan(changes=sorted(export.payload), deletes=list(journal["deletes"]))
    drift: list[str] = []
    full = options.full_check or export.mode == "full"
    if options.tree_check and not full:  # a full post-check already compared everything
        drift = tree.differences(export.inventory, tree.scan_tree(target))
        if drift:
            log_event(
                logging.WARNING,
                "tree differs from inventory",
                slug=export.slug,
                export_id=export.export_id,
                differences=len(drift),
                first=drift[: tree.SHOWN_DIFFERENCES],
            )
    return Outcome(export.export_id, "applied", plan, drift)


def warn_on_version_mismatch(export: Export) -> bool:
    """Warn when the export was written by another commit than this importer's.

    Only a warning: the export contract is versioned by format_version, but a sender
    and importer from different commits may disagree on rules such as superseded
    exports. Returns True if a warning was logged.
    """
    own = buildinfo.commit()
    sender = export.sender_commit
    if own is None or sender is None or own == sender:
        return False
    log_event(
        logging.WARNING,
        "importer and sender were built from different commits",
        slug=export.slug,
        export_id=export.export_id,
        importer_commit=own,
        sender_commit=sender,
    )
    return True


def _superseded(export: Export, state: RepoState, options: Options) -> bool:
    """Skip an unapplied export older than the last applied one, unless it is a full
    export the operator explicitly applies with --rebase."""
    if options.rebase and export.mode == "full":
        return False
    return state.is_superseded_by_state(export.export_id)


def _record_superseded(export: Export, store: StateStore, state: RepoState) -> None:
    state.history.append(
        {
            "export_id": export.export_id,
            "status": "superseded",
            "mode": export.mode,
            "base_export_id": export.base_export_id,
            "skipped_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "last_applied": state.last_export_id,
        }
    )
    store.save(state)
    log_event(
        logging.WARNING,
        "skipped superseded export",
        slug=export.slug,
        export_id=export.export_id,
        last_applied=state.last_export_id,
    )


def skip_jumped(export_dir: Path, config: Config, options: Options, jumped_by: str) -> Outcome:
    """Record an export as superseded because a later pending export starts at the
    receiver's current state (an "incremental since" export). Only the export's
    metadata is checked; nothing in the tree changes."""
    directory = Path(os.path.abspath(export_dir))
    try:
        metadata = load_metadata(directory)
        store = StateStore(config.state_dir, metadata.slug)
        if options.dry_run:
            return Outcome(metadata.export_id, "superseded", Plan())
        with store.locked():
            state = store.load()
            if state.applied(metadata.export_id) or state.superseded(metadata.export_id):
                return Outcome(metadata.export_id, "superseded", Plan())
            state.history.append(
                {
                    "export_id": metadata.export_id,
                    "status": "superseded",
                    "mode": metadata.mode,
                    "base_export_id": metadata.base_export_id,
                    "skipped_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "last_applied": state.last_export_id,
                    "superseded_by": jumped_by,
                }
            )
            store.save(state)
    except ImporterError:
        raise
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc
    log_event(
        logging.WARNING,
        "skipped superseded export",
        slug=metadata.slug,
        export_id=metadata.export_id,
        superseded_by=jumped_by,
    )
    return Outcome(metadata.export_id, "superseded", Plan())


def import_export(export_dir: Path, config: Config, options: Options) -> Outcome:
    """Import one export directory. Importing an already applied export is a no-op.

    An unapplied export older than the last applied one is superseded: a later export
    built on an earlier base (or a full with --rebase) already covers it. It is skipped
    and recorded in the history instead of failing the chain check.
    """
    directory = Path(os.path.abspath(export_dir))
    try:
        export = load_export(directory)
        check_signature(directory, config.keyring, config.require_signature)
        warn_on_version_mismatch(export)
        store = StateStore(config.state_dir, export.slug)
        target = config.mirror_root / export.slug

        if options.dry_run:
            state = store.load()
            if state.applied(export.export_id):
                return Outcome(export.export_id, "noop", Plan())
            if state.superseded(export.export_id) or _superseded(export, state, options):
                return Outcome(export.export_id, "superseded", Plan())
            check_chain(state, export, bool(_tree_paths(target)), options.rebase)
            replace = options.rebase and export.mode == "full"
            return Outcome(export.export_id, "dry-run", build_plan(export, target, replace))

        with store.locked():
            state = store.load()
            if state.applied(export.export_id):
                shutil.rmtree(config.staging_dir / export.slug, ignore_errors=True)
                log_event(
                    logging.INFO,
                    "already applied",
                    slug=export.slug,
                    export_id=export.export_id,
                )
                return Outcome(export.export_id, "noop", Plan())
            if state.superseded(export.export_id):
                return Outcome(export.export_id, "superseded", Plan())
            if _superseded(export, state, options):
                _record_superseded(export, store, state)
                return Outcome(export.export_id, "superseded", Plan())
            return _apply_locked(export, config, options, store, state)
    except ImporterError:
        raise
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc
