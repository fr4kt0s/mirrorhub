"""Plan, stage, apply and post-check an export against a local mirror tree.

Flow: verify -> signature -> lock -> chain check -> plan -> stream into staging ->
sync -> journal -> deletes -> per-file replace -> post-check -> state update -> cleanup.
A failure after the journal exists is rolled forward by running the same import again.
The export directory is only ever read.

Every byte is hashed once on its way into staging (archive parts against
MANIFEST.sha256, files against files.json). The post-check compares file system
metadata; reading the tree back and hashing it again is --full-check.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import posixpath
import shutil
import stat
import tarfile
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
    verify_parts,
)
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import (
    ChainError,
    ConfigError,
    ImporterError,
    IoFailure,
    PostCheckError,
    UnsafeArchiveError,
    VerificationFailed,
)
from mirrorhub.importer.extract import extract_payload
from mirrorhub.importer.logs import log_event
from mirrorhub.importer.permissions import PermissionFailure, explain, preflight, report
from mirrorhub.importer.progress import Progress
from mirrorhub.importer.reader import open_archive
from mirrorhub.importer.state import RepoState, StateStore


@dataclass(frozen=True)
class Options:
    """Options of one import run (command-line flags of ``import``)."""

    dry_run: bool = False
    rebase: bool = False
    # Re-read and hash the whole tree after applying (otherwise: metadata only).
    full_check: bool = False
    discard_staging: bool = False
    # Quick comparison of the whole tree with files.json after each import.
    tree_check: bool = True
    # Hash the archive parts before extracting (otherwise while streaming).
    verify_first: bool = False


@dataclass
class Plan:
    """What applying an export changes in the tree: paths added, replaced and deleted."""

    adds: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    """Result of handling one export: its status, plan and tree differences."""

    export_id: str
    status: str
    plan: Plan
    # Differences between the tree and files.json found after applying (paths the
    # export did not touch, e.g. deleted by hand). The export itself was applied.
    drift: list[str] = field(default_factory=list)
    tree_checked: bool = False
    # The post-check compared the whole tree (full or repair export, or --full-check).
    whole_tree_checked: bool = False
    # A repair export (manifest field repair, #43).
    repair: bool = False


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


def _tree_has_entries(root: Path) -> bool:
    """Return True as soon as one file or symlink is found (same rules as _tree_paths)."""
    if not root.is_dir():
        return False
    for directory, names, filenames in os.walk(root, followlinks=False):
        if filenames:
            return True
        base = Path(directory)
        if any((base / name).is_symlink() for name in names):
            return True
        names[:] = [n for n in names if n not in SKIP_DIRECTORIES]
    return False


def build_plan(export: Export, target: Path, replace: bool) -> Plan:
    """Plan the changes an export makes to the tree at target.

    Payload paths that exist are changes, the others adds; deleted.txt entries that
    exist are deletes. With replace (``--rebase``), every path not in files.json is
    deleted as well, so the tree ends up exactly as the export describes it.
    """
    plan = Plan()
    for relative in sorted(export.payload):
        (plan.changes if os.path.lexists(target / relative) else plan.adds).append(relative)
    deletes = {path for path in export.deleted if os.path.lexists(target / path)}
    if replace:
        deletes |= _tree_paths(target) - set(export.inventory)
    plan.deletes = sorted(deletes)
    return plan


def check_chain(state: RepoState, export: Export, tree_has_entries: bool, rebase: bool) -> None:
    """Raise ChainError unless the export may be applied on top of the current state.

    A full export needs an empty state and tree, or ``--rebase``. An incremental
    export must start at the last applied export (its ``base_export_id``).
    """
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


def _check_entry(root: Path, relative: str, entry: dict[str, Any], hash_contents: bool) -> None:
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
        if hash_contents and _file_digest(path) != entry["sha256"]:
            raise PostCheckError(f"sha256 mismatch: {relative}")
    if info.st_mtime_ns != entry["mtime_ns"]:
        raise PostCheckError(f"mtime mismatch: {relative}")


def post_check(
    export: Export,
    target: Path,
    journal_deletes: list[str],
    *,
    whole_tree: bool,
    hash_contents: bool,
    tolerate_extra: bool = False,
) -> list[str]:
    """Check that the resulting tree matches files.json (touched paths or whole tree).

    Type, size, mtime and symlink targets are always compared. Contents were hashed
    while staging; hash_contents (--full-check) reads every file back and hashes it
    again, which also finds damage between writing and reading.

    With tolerate_extra (repair exports) paths that are not in files.json do not fail
    the check; they are returned as differences ("extra: <path>"), e.g. extra files
    the operator chose to keep.
    """
    for relative in journal_deletes:
        if os.path.lexists(target / relative):
            raise PostCheckError(f"deleted path still exists: {relative}")
    slug, export_id = export.slug, export.export_id
    if not whole_tree:
        progress = Progress(
            "post-check", files_total=len(export.payload), slug=slug, export_id=export_id
        )
        for relative in sorted(export.payload):
            _check_entry(target, relative, export.inventory[relative], hash_contents)
            progress.advance()
        progress.finish()
        return []
    entries = tree.scan(target, export.inventory, slug=slug, export_id=export_id)
    found = tree.differences(export.inventory, entries)
    extra = [line for line in found if line.startswith("extra: ")] if tolerate_extra else []
    found = [line for line in found if line not in extra]
    if not found and hash_contents:
        found = tree.hash_contents(export.inventory, target, slug=slug, export_id=export_id)
    if found:
        raise PostCheckError(tree.summary(f"files.json of {export.export_id}", found))
    return extra


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


def _sync_filesystem(path: Path) -> None:
    """Flush the file system holding path to disk.

    Uses syncfs(2) where available (Linux), else sync(2) for all file systems.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        syncfs = libc.syncfs
    except (OSError, AttributeError):
        os.sync()
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        if syncfs(descriptor) != 0:
            os.sync()
    finally:
        os.close(descriptor)


def apply_journal(
    target: Path, files: Path, journal: dict[str, Any], progress: Progress | None = None
) -> None:
    """Idempotent: deletes first, then per-file replace from staging.

    Parent directories of installs are checked (and created) once per directory;
    after the deletes nothing removes directories any more.
    """
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
        if progress is not None:
            progress.advance()
    checked: set[str] = set()
    for relative in journal["installs"]:
        source = files / relative
        if not os.path.lexists(source):
            continue
        parent = posixpath.dirname(relative)
        if parent not in checked:
            _parents(target, relative, create=True)
            while parent and parent not in checked:
                checked.add(parent)
                parent = posixpath.dirname(parent)
        destination = target / relative
        if destination.is_dir() and not destination.is_symlink():
            raise IoFailure(f"cannot install over a directory: {relative}")
        os.replace(source, destination)
        if progress is not None:
            progress.advance()


def _clear_staging(staging: Path) -> None:
    """Remove a previous staging directory.

    Refuses (exit 9) if entries cannot be removed, e.g. left behind by a run as
    another user (issue #60).
    """
    failed: list[tuple[str, BaseException]] = []

    def collect(function: Any, path: str, exc: BaseException) -> None:
        failed.append((path, exc))

    if os.path.lexists(staging):
        shutil.rmtree(staging, onexc=collect)
    if failed:
        path, exc = failed[0]
        error = exc if isinstance(exc, PermissionError) else PermissionError(13, str(exc), path)
        if error.filename is None:
            error.filename = path
        raise explain(
            error,
            "clearing the staging directory of an earlier run",
            f"  {len(failed)} entr{'y' if len(failed) == 1 else 'ies'} in {staging} cannot be "
            "removed by this user; remove the directory as its owner or root, e.g. "
            f"sudo rm -rf {staging}",
        )


def _remove_staging(staging: Path, slug: str) -> None:
    """Remove staging after a finished or failed run.

    A leftover is only a warning; the next run refuses it with an explanation.
    """
    shutil.rmtree(staging, ignore_errors=True)
    if os.path.lexists(staging):
        log_event(
            logging.WARNING,
            "staging directory not fully removed",
            slug=slug,
            staging=staging,
        )


INSTALL_HINT = (
    "  the mirror tree may be partly updated: fix the permissions, then run the same "
    "import again (its journal resumes it); do not use --discard-staging"
)


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
        target_has_entries = _tree_has_entries(target)
        check_chain(state, export, target_has_entries, options.rebase)
        log_event(
            logging.DEBUG,
            "chain check passed",
            slug=export.slug,
            export_id=export.export_id,
            mode=export.mode,
            base_export_id=export.base_export_id,
            last_applied=state.last_export_id,
            rebase=replace,
        )

    plan = Plan()
    if resuming:
        assert journal is not None
        deletes, installs = list(journal["deletes"]), list(journal["installs"])
    else:
        plan = build_plan(export, target, replace)
        deletes, installs = plan.deletes, [*plan.adds, *plan.changes]
    problems = preflight(target, config.staging_dir, deletes, installs)
    log_event(
        logging.DEBUG,
        "permission check",
        slug=export.slug,
        export_id=export.export_id,
        problems=len(problems),
    )
    if problems:
        raise PermissionFailure(report(problems, target))

    target.mkdir(parents=True, exist_ok=True)
    config.staging_dir.mkdir(parents=True, exist_ok=True)
    if not _same_filesystem(config.staging_dir, target):
        raise ConfigError("staging_dir must be on the same filesystem as the mirror tree")

    if resuming:
        log_event(
            logging.WARNING,
            "resuming interrupted import",
            slug=export.slug,
            export_id=export.export_id,
        )
    else:
        log_event(
            logging.DEBUG,
            "plan",
            slug=export.slug,
            export_id=export.export_id,
            adds=len(plan.adds),
            changes=len(plan.changes),
            deletes=len(plan.deletes),
        )
        _clear_staging(staging)
        files.mkdir(parents=True)
        log_event(
            logging.DEBUG,
            "extracting archive",
            slug=export.slug,
            export_id=export.export_id,
            parts=len(export.parts),
            compressed=export.compressed,
            staging=files,
        )
        try:
            _extract(export, files, options)
        except PermissionError as exc:
            _remove_staging(staging, export.slug)
            raise explain(exc, "writing to the staging directory") from exc
        except BaseException:
            _remove_staging(staging, export.slug)
            raise
        # Staged data must be on disk before the journal makes it "to be installed".
        _sync_filesystem(files)
        log_event(logging.DEBUG, "staging synced", slug=export.slug, export_id=export.export_id)
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

    assert journal is not None
    installing = Progress(
        "installing",
        files_total=len(journal["deletes"]) + len(journal["installs"]),
        slug=export.slug,
        export_id=export.export_id,
    )
    try:
        apply_journal(target, files, journal, installing)
    except PermissionError as exc:
        raise explain(exc, "installing into the mirror tree", INSTALL_HINT) from exc
    installing.finish()
    log_event(
        logging.DEBUG,
        "journal applied",
        slug=export.slug,
        export_id=export.export_id,
        deletes=len(journal["deletes"]),
        installs=len(journal["installs"]),
        target=target,
    )
    # a repair export must leave the whole tree matching files.json (#43)
    full_post_check = options.full_check or export.mode == "full" or export.repair is not None
    try:
        kept = post_check(
            export,
            target,
            journal["deletes"],
            whole_tree=full_post_check,
            hash_contents=options.full_check,
            tolerate_extra=export.repair is not None,
        )
    except PostCheckError:
        _remove_staging(staging, export.slug)
        raise
    log_event(
        logging.DEBUG,
        "post-check passed",
        slug=export.slug,
        export_id=export.export_id,
        scope="whole tree" if full_post_check else "touched paths",
        contents="re-hashed" if options.full_check else "hashed while staging",
    )

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
            **({"repair": True} if export.repair is not None else {}),
        }
    )
    store.save(state)
    _remove_staging(staging, export.slug)
    log_event(logging.INFO, "applied", slug=export.slug, export_id=export.export_id)
    if resuming:
        plan = Plan(changes=sorted(export.payload), deletes=list(journal["deletes"]))
    if export.repair is not None:
        log_event(
            logging.INFO,
            "repair export applied, whole tree checked",
            slug=export.slug,
            export_id=export.export_id,
            resent=len(export.payload),
            deleted=len(journal["deletes"]),
            extra_kept=len(kept),
        )
    drift: list[str] = kept  # extra paths a repair export left in place
    checked = options.tree_check and not full_post_check  # that compared everything
    if checked:
        entries = tree.scan(target, export.inventory, slug=export.slug, export_id=export.export_id)
        drift = tree.differences(export.inventory, entries)
        log_tree_check(export.slug, export.export_id, len(export.inventory), drift)
    return Outcome(
        export.export_id,
        "applied",
        plan,
        drift,
        checked,
        full_post_check,
        repair=export.repair is not None,
    )


def log_tree_check(slug: str, export_id: str, entries: int, drift: list[str]) -> None:
    """Log the result of the quick tree check at debug level."""
    log_event(
        logging.DEBUG,
        "tree check done",
        slug=slug,
        export_id=export_id,
        entries=entries,
        differences=len(drift),
    )
    if drift:
        log_event(
            logging.WARNING,
            "tree differs from inventory",
            slug=slug,
            export_id=export_id,
            differences=len(drift),
            first=drift[: tree.SHOWN_DIFFERENCES],
        )


def _extract(export: Export, files: Path, options: Options) -> None:
    """Stream the archive into staging.

    Without --verify-first the archive parts were not hashed yet. If extraction
    fails, they are hashed now so that a damaged archive is reported as such
    (verification failure) and not as whatever the damage looked like to tar or zstd.
    """
    staged = [export.inventory[path] for path in export.payload]
    progress = Progress(
        "staging",
        files_total=len(staged),
        bytes_total=sum(entry["size"] for entry in staged if entry["type"] == "file"),
        slug=export.slug,
        export_id=export.export_id,
    )
    try:
        with open_archive(export) as stream:
            extract_payload(export, stream, files, progress)
        progress.finish()
    except VerificationFailed:
        raise
    except (UnsafeArchiveError, tarfile.TarError, EOFError) as exc:
        if not options.verify_first:
            log_event(
                logging.DEBUG,
                "extraction failed, hashing archive parts",
                slug=export.slug,
                export_id=export.export_id,
                error=str(exc),
            )
            verify_parts(export)
        if isinstance(exc, UnsafeArchiveError):
            raise
        raise UnsafeArchiveError(f"unreadable archive: {exc}") from exc


def warn_on_version_mismatch(export: Export) -> bool:
    """Warn when the export was written by another commit than this importer's.

    Only a warning: the export contract is versioned by format_version, but a sender
    and importer from different commits may disagree on rules such as superseded
    exports. Returns True if a warning was logged.
    """
    own = buildinfo.commit()
    sender = export.sender_commit
    if own is None or sender is None or buildinfo.same_commit(own, sender):
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
    """Return True if an unapplied export is older than the last applied one.

    Such an export is skipped, unless it is a full export the operator explicitly
    applies with --rebase.
    """
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
    """Record an export as superseded by a later pending export.

    The later export starts at the receiver's current state (an "incremental since"
    export). Only the export's metadata is checked; nothing in the tree changes.
    """
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
    except PermissionError as exc:
        raise explain(exc, "importing (tree check, state or inventory files)") from exc
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
        export = load_export(directory, verify_parts_first=options.verify_first)
        check_signature(directory, config.keyring, config.require_signature)
        warn_on_version_mismatch(export)
        store = StateStore(config.state_dir, export.slug)
        target = config.mirror_root / export.slug

        if options.dry_run:
            log_event(logging.DEBUG, "dry run, no lock taken", slug=export.slug)
            state = store.load()
            if state.applied(export.export_id):
                return Outcome(export.export_id, "noop", Plan())
            if state.superseded(export.export_id) or _superseded(export, state, options):
                return Outcome(export.export_id, "superseded", Plan())
            check_chain(state, export, _tree_has_entries(target), options.rebase)
            replace = options.rebase and export.mode == "full"
            plan = build_plan(export, target, replace)
            problems = preflight(
                target, config.staging_dir, plan.deletes, [*plan.adds, *plan.changes]
            )
            if problems:
                raise PermissionFailure("dry run: " + report(problems, target))
            return Outcome(export.export_id, "dry-run", plan, repair=export.repair is not None)

        with store.locked():
            log_event(logging.DEBUG, "lock acquired", slug=export.slug, lock=store.lock_path)
            state = store.load()
            if state.applied(export.export_id):
                _remove_staging(config.staging_dir / export.slug, export.slug)
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
    except PermissionError as exc:
        raise explain(exc, "importing (tree check, state or inventory files)") from exc
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc
