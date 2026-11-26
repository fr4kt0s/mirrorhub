"""Command line interface: python -m mirrorhub.importer."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from mirrorhub import buildinfo
from mirrorhub.importer import events, permissions, progress, tree
from mirrorhub.importer.adopt import adopt
from mirrorhub.importer.apply import (
    Options,
    Outcome,
    import_export,
    log_tree_check,
    skip_jumped,
)
from mirrorhub.importer.check import check_tree
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config, load_config
from mirrorhub.importer.errors import ConfigError, DriftError, ImporterError
from mirrorhub.importer.logs import FORMATS, configure_logging, log_event
from mirrorhub.importer.state import StateStore

CONFIG_ENV = "MIRRORHUB_IMPORTER_CONFIG"


LOG_LEVELS = ("debug", "info", "warning", "error")


def _interval(value: str) -> float:
    seconds = float(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be 0 (off) or a positive number of seconds")
    return seconds


PROGRESS_HELP = (
    "seconds between JSON progress lines on stderr during long phases (staging, "
    f"installing, tree scans, hashing); 0 turns them off (default: {progress.DEFAULT_INTERVAL:g})"
)
LOG_FORMAT_HELP = (
    "log lines on stderr as readable text or JSON; auto (default): text on a terminal, "
    "JSON otherwise (journal, file, pipe)"
)
LOG_LEVEL_HELP = (
    "level of the log lines on stderr (default: info); does not change the "
    "plain-text output on stdout"
)


def _global_options(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
    """--config and --log-level, accepted before and after the command.

    Only the main parser sets defaults; on the command parsers they are suppressed, so
    a value given before the command is kept unless the command repeats the option.
    """
    parser.add_argument(
        "--config",
        type=Path,
        default=None if defaults else argparse.SUPPRESS,
        help=f"TOML file (or set {CONFIG_ENV})",
    )
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=LOG_LEVELS,
        default="info" if defaults else argparse.SUPPRESS,
        help=LOG_LEVEL_HELP,
    )
    parser.add_argument(
        "--log-format",
        type=str.lower,
        choices=FORMATS,
        default="auto" if defaults else argparse.SUPPRESS,
        help=LOG_FORMAT_HELP,
    )
    parser.add_argument(
        "--progress-interval",
        type=_interval,
        metavar="SECONDS",
        default=progress.DEFAULT_INTERVAL if defaults else argparse.SUPPRESS,
        help=PROGRESS_HELP,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirrorhub.importer", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {buildinfo.describe()}")
    _global_options(parser, defaults=True)
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, defaults=False)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "import", parents=[common], help="apply exports from the configured inbox"
    )
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--slug", help="apply all pending exports of this repository")
    target.add_argument("--export", type=Path, help="apply one export directory")
    target.add_argument(
        "--all",
        dest="all_slugs",
        action="store_true",
        help="apply all pending exports of every repository in the inbox",
    )
    run.add_argument("--dry-run", action="store_true", help="verify and print the plan only")
    run.add_argument("--rebase", action="store_true", help="allow a full export over existing data")
    run.add_argument(
        "--full-check",
        action="store_true",
        help="read back and re-hash the whole tree afterwards (contents are hashed while "
        "staging; without this option the post-check compares metadata only)",
    )
    run.add_argument(
        "--verify-first",
        action="store_true",
        help="hash the archive parts before extracting (reads the archive twice; "
        "by default they are hashed once, while extracting)",
    )
    run.add_argument(
        "--no-tree-check",
        action="store_true",
        help="skip the quick comparison of the whole tree with files.json after importing "
        "(with --slug/--all: once, after the last pending export of a repository)",
    )
    run.add_argument(
        "--drift-output",
        type=Path,
        metavar="DIR",
        help="write the differences found after importing to DIR/<slug>/<kind>.txt",
    )
    run.add_argument(
        "--discard-staging", action="store_true", help="drop another unfinished import"
    )

    status = commands.add_parser("status", parents=[common], help="show receiver state")
    status.add_argument("--slug", help="repository slug (default: all)")

    check = commands.add_parser(
        "check",
        parents=[common],
        help="compare the tree with the inventory of its last applied export",
    )
    scope = check.add_mutually_exclusive_group(required=True)
    scope.add_argument("--slug", help="repository slug")
    scope.add_argument(
        "--all", dest="all_slugs", action="store_true", help="every repository with a state"
    )
    check.add_argument(
        "--hash", action="store_true", help="also compare SHA-256 (reads every file once)"
    )
    check.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIR",
        help="write all differences to DIR/<slug>/<kind>.txt plus summary.txt",
    )

    take = commands.add_parser(
        "adopt",
        parents=[common],
        help="write a lost state file for a tree that matches an export exactly",
    )
    take.add_argument("--slug", required=True, help="repository slug")
    which = take.add_mutually_exclusive_group(required=True)
    which.add_argument("--export-id", help="export in the inbox whose files.json the tree matches")
    which.add_argument(
        "--auto", action="store_true", help="try the published exports in the inbox, newest first"
    )
    take.add_argument(
        "--hash", action="store_true", help="also compare SHA-256 (reads every file once)"
    )
    web = commands.add_parser(
        "serve",
        parents=[common],
        help="web UI for all commands (no login by default; see docs/importer-ui.md)",
    )
    web.add_argument(
        "--listen",
        default=os.environ.get("MIRRORHUB_IMPORTER_UI_LISTEN", "127.0.0.1:8080"),
        metavar="HOST:PORT",
        help="address to listen on (default 127.0.0.1:8080 or MIRRORHUB_IMPORTER_UI_LISTEN)",
    )
    return parser


def pending_exports(config: Config, slug: str) -> list[Path]:
    """Published exports (with .done) in the inbox, oldest first."""
    if not SLUG_RE.fullmatch(slug):
        raise ConfigError(f"invalid slug: {slug!r}")
    directory = config.export_dir / slug
    if not directory.is_dir():
        raise ConfigError(f"no inbox directory for {slug!r}: {directory}")
    return [
        child
        for child in sorted(directory.iterdir())
        if EXPORT_ID_RE.fullmatch(child.name)
        and child.is_dir()
        and not child.is_symlink()
        and (child / ".done").is_file()
    ]


def list_slugs(config: Config) -> list[str]:
    """Repository directories in the inbox; names that are not valid slugs are skipped."""
    if not config.export_dir.is_dir():
        raise ConfigError(f"export_dir does not exist: {config.export_dir}")
    slugs: list[str] = []
    for child in sorted(config.export_dir.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue  # reserved sender directories such as _events (event files)
        if not SLUG_RE.fullmatch(child.name):
            log_event(
                logging.WARNING,
                "skipping inbox directory, not a valid slug",
                name=child.name,
            )
            continue
        slugs.append(child.name)
    return slugs


SHOWN_DIFFERENCES = 50


def _print_differences(differences: list[str]) -> None:
    for line in differences[:SHOWN_DIFFERENCES]:
        print(f"  {line}")
    if len(differences) > SHOWN_DIFFERENCES:
        print(f"  ... and {len(differences) - SHOWN_DIFFERENCES} more")


def _print(outcome: Outcome, dry_run: bool) -> None:
    plan = outcome.plan
    if dry_run and outcome.status == "dry-run":
        for prefix, paths in (("A", plan.adds), ("C", plan.changes), ("D", plan.deletes)):
            for path in paths:
                print(f"{prefix} {path}")
    print(
        f"{outcome.status} {outcome.export_id}: "
        f"{len(plan.adds)} added, {len(plan.changes)} changed, {len(plan.deletes)} deleted"
    )
    _print_drift(outcome.export_id, outcome.drift)


def _print_drift(export_id: str, drift: list[str]) -> None:
    if drift:
        print(
            f"warning: the tree differs from files.json of {export_id} "
            f"in {len(drift)} places (not caused by the import):"
        )
        _print_differences(drift)


def _base_of(path: Path) -> str | None:
    """base_export_id from manifest.json, or None if it cannot be read."""
    try:
        value = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base = value.get("base_export_id") if isinstance(value, dict) else None
    return base if isinstance(base, str) and base else None


def _jumped_by(path: Path, pending: list[Path], last: str | None) -> str | None:
    """A later pending export that starts at the current state skips this one."""
    if last is None:
        return None
    for later in pending:
        if later.name > path.name and _base_of(later) == last:
            return later.name
    return None


def _mode_of(export_id: str) -> str:
    return export_id.rsplit("-", 1)[-1]


def _emit_outcome(
    slug: str,
    path: Path,
    outcome: Outcome,
    started: float,
    options: Options,
    jumped_by: str | None = None,
) -> None:
    """Event files for one export: applied (plus tree verified), superseded."""
    if options.dry_run:
        return
    if outcome.status == "superseded":
        events.emit("import_superseded", slug, export_id=outcome.export_id, superseded_by=jumped_by)
    if outcome.status != "applied":
        return
    plan = outcome.plan
    events.emit(
        "import_applied",
        slug,
        export_id=outcome.export_id,
        export_mode=_mode_of(outcome.export_id),
        base_export_id=_base_of(path),
        payload_count=len(plan.adds) + len(plan.changes),
        deleted_count=len(plan.deletes),
        duration_s=round(time.monotonic() - started, 1),
        tree_verified=outcome.whole_tree_checked,
    )
    if outcome.whole_tree_checked:  # the post-check compared the whole tree
        _emit_tree_check(slug, outcome.export_id, "post-check", [], hashed=options.full_check)
    elif outcome.tree_checked:
        _emit_tree_check(slug, outcome.export_id, "import", outcome.drift, hashed=False)


def _emit_tree_check(
    slug: str,
    export_id: str,
    origin: str,
    found: list[str],
    *,
    hashed: bool,
    entries: int | None = None,
) -> None:
    events.emit(
        "tree_check",
        slug,
        export_id=export_id,
        origin=origin,
        hashed=hashed,
        differences=len(found),
        counts=events.count_kinds(found),
        entries=entries,
    )


def _emit_failure(kind: str, slug: str | None, exc: ImporterError, **fields: object) -> None:
    """One failure event per error, even if it passes several handlers."""
    if getattr(exc, "event_written", False):
        return
    events.emit(
        kind,
        slug,
        error=str(exc),
        error_kind=type(exc).__name__,
        exit_code=exc.exit_code,
        **fields,
    )
    exc.event_written = True  # type: ignore[attr-defined]


def _report(
    directory: Path | None,
    slug: str,
    export_id: str,
    drift: list[str],
    *,
    hashed: bool = False,
) -> None:
    if directory is None:
        return
    written = tree.write_report(directory, slug, export_id, drift, hashed=hashed)
    print(f"differences written to {written}")


def _tree_check_after(config: Config, slug: str, report: Path | None) -> bool:
    """Quick comparison of the tree with the last applied export; True if it differs."""
    result = check_tree(config, slug)
    log_tree_check(slug, result.export_id, result.entries, result.differences)
    _emit_tree_check(
        slug, result.export_id, "import", result.differences, hashed=False, entries=result.entries
    )
    _print_drift(result.export_id, result.differences)
    _report(report, slug, result.export_id, result.differences)
    return bool(result.differences)


def _import_slug(config: Config, slug: str, options: Options, report: Path | None = None) -> bool:
    """Apply all pending exports of one repository; True if the tree differs afterwards.

    Exports are taken oldest first. One is skipped as superseded when it is older than
    the last applied export, or when a later pending export starts at the current state
    (an "incremental since" export that replaces it).

    The quick tree check runs once, after the last applied export, not after each one:
    every walk of a large tree costs one lstat per file. It is skipped when the last
    applied export's post-check already compared the whole tree (full export).

    With events_dir set, every applied or superseded export, the tree check and a
    failure (with the export it happened on) are written as event files.
    """
    store = StateStore(config.state_dir, slug)
    done = 0
    last_applied: Outcome | None = None
    tree_check = options.tree_check
    options = dataclasses.replace(options, tree_check=False)
    current: str | None = None
    try:
        pending = pending_exports(config, slug)
        log_event(
            logging.DEBUG,
            "pending exports",
            slug=slug,
            count=len(pending),
            export_ids=[path.name for path in pending],
        )
        for path in pending:
            state = store.load()
            if state.applied(path.name) or state.superseded(path.name):
                log_event(
                    logging.DEBUG,
                    "export already in history",
                    slug=slug,
                    export_id=path.name,
                    status="applied" if state.applied(path.name) else "superseded",
                )
                continue
            current = path.name
            started = time.monotonic()
            jumped_by = _jumped_by(path, pending, state.last_export_id)
            if jumped_by is not None:
                outcome = skip_jumped(path, config, options, jumped_by)
            else:
                outcome = import_export(path, config, options)
            _print(outcome, options.dry_run)
            _emit_outcome(slug, path, outcome, started, options, jumped_by)
            current = None
            done += 1
            if outcome.status == "applied":
                last_applied = outcome
            if outcome.status == "superseded":
                continue  # recorded (or, in a dry run, reported) and skipped
            if options.dry_run:
                log_event(
                    logging.INFO,
                    "dry-run plans only the first pending export",
                    slug=slug,
                )
                break
            options = dataclasses.replace(options, rebase=False, discard_staging=False)
        if done == 0:
            print(f"nothing to import for {slug}")
        if tree_check and last_applied is not None and not last_applied.whole_tree_checked:
            return _tree_check_after(config, slug, report)
        if last_applied is not None and last_applied.whole_tree_checked:
            # the post-check compared the whole tree: replace an older report with an empty one
            _report(report, slug, last_applied.export_id, [], hashed=options.full_check)
        return False
    except ImporterError as exc:
        if not options.dry_run:
            _emit_failure("import_failed", slug, exc, export_id=current)
        raise


def _import_all(config: Config, options: Options, report: Path | None = None) -> int:
    """Process every repository in the inbox; one failing slug does not stop the others."""
    if options.rebase or options.discard_staging:
        raise ConfigError("--rebase and --discard-staging cannot be used with --all")
    slugs = list_slugs(config)
    log_event(logging.DEBUG, "repositories in the inbox", slugs=slugs)
    if not slugs:
        print("no repositories in the inbox")
    worst = 0
    for slug in slugs:
        try:
            if _import_slug(config, slug, options, report):
                worst = max(worst, DriftError.exit_code)
        except ImporterError as exc:
            log_event(
                logging.ERROR,
                "slug failed",
                slug=slug,
                error=str(exc),
                kind=type(exc).__name__,
            )
            print(f"{slug}: error: {exc}", file=sys.stderr)
            worst = max(worst, exc.exit_code)
    return worst


def _run_import(config: Config, arguments: argparse.Namespace) -> int:
    options = Options(
        dry_run=arguments.dry_run,
        rebase=arguments.rebase,
        full_check=arguments.full_check,
        discard_staging=arguments.discard_staging,
        tree_check=not arguments.no_tree_check,
        verify_first=arguments.verify_first,
    )
    report = arguments.drift_output or config.reports_dir
    if arguments.export is not None:
        path = Path(os.path.abspath(arguments.export))
        slug = path.parent.name
        started = time.monotonic()
        try:
            outcome = import_export(path, config, options)
        except ImporterError as exc:
            if not options.dry_run:
                _emit_failure("import_failed", slug, exc, export_id=path.name)
            raise
        _print(outcome, options.dry_run)
        _emit_outcome(slug, path, outcome, started, options)
        if outcome.tree_checked:
            _report(report, slug, outcome.export_id, outcome.drift)
        elif outcome.whole_tree_checked:
            _report(report, slug, outcome.export_id, [], hashed=options.full_check)
        return DriftError.exit_code if outcome.drift else 0
    if arguments.all_slugs:
        return _import_all(config, options, report)
    drift = _import_slug(config, arguments.slug, options, report)
    return DriftError.exit_code if drift else 0


def _run_import_with_event(config: Config, arguments: argparse.Namespace) -> int:
    """_run_import plus one import_run event per run (also when nothing was pending), so
    monitoring can tell a stopped timer from an idle inbox."""
    started = time.monotonic()
    code = 1  # an unexpected exception ends the process with exit code 1
    try:
        code = _run_import(config, arguments)
        return code
    except ImporterError as exc:
        code = exc.exit_code
        raise
    finally:
        if not arguments.dry_run:
            scope = (
                "all"
                if arguments.all_slugs
                else f"export {arguments.export}"
                if arguments.export is not None
                else f"slug {arguments.slug}"
            )
            events.emit(
                "import_run",
                _slug_of(arguments),
                scope=scope,
                exit_code=code,
                duration_s=round(time.monotonic() - started, 1),
            )


def _state_slugs(config: Config) -> list[str]:
    return sorted(path.stem for path in config.state_dir.glob("*.json"))


def _check(config: Config, arguments: argparse.Namespace) -> int:
    """Exit 0 if every checked tree matches, 11 if one differs, else the worst error."""
    slugs = _state_slugs(config) if arguments.all_slugs else [arguments.slug]
    log_event(logging.DEBUG, "checking trees", slugs=slugs, hashed=arguments.hash)
    if not slugs:
        print("no repositories with a receiver state")
    worst = 0
    for slug in slugs:
        try:
            result = check_tree(config, slug, hash_contents=arguments.hash)
        except ImporterError as exc:
            _emit_failure("check_failed", slug, exc)
            if not arguments.all_slugs:
                raise
            print(f"{slug}: error: {exc}", file=sys.stderr)
            worst = max(worst, exc.exit_code)
            continue
        kind = "quick check and SHA-256" if result.hashed else "quick check"
        _warn_permissions(config, slug, result.directories)
        _emit_tree_check(
            slug,
            result.export_id,
            "check",
            result.differences,
            hashed=result.hashed,
            entries=result.entries,
        )
        output_dir = arguments.output_dir or config.reports_dir
        if output_dir is not None:
            written = tree.write_report(
                output_dir,
                slug,
                result.export_id,
                result.differences,
                hashed=result.hashed,
            )
            print(f"{slug}: report written to {written}")
        if not result.differences:
            print(f"ok {slug}: {result.entries} entries match {result.export_id} ({kind})")
            continue
        print(
            f"differs {slug}: {len(result.differences)} differences from "
            f"{result.export_id} ({kind}):"
        )
        _print_differences(result.differences)
        log_event(
            logging.WARNING,
            "tree differs from inventory",
            slug=slug,
            export_id=result.export_id,
            differences=len(result.differences),
        )
        worst = max(worst, DriftError.exit_code)
    return worst


def _warn_permissions(config: Config, slug: str, directories: list[Path]) -> None:
    """check/adopt: warn if the tree has directories the importer's user cannot
    change (the next import touching them would stop at the permission check)."""
    try:
        warning = permissions.tree_warning(config.mirror_root / slug, directories)
    except OSError:
        return
    if warning is None:
        return
    print(warning)
    log_event(
        logging.WARNING,
        "tree has directories this user cannot change",
        slug=slug,
        user=permissions.who(),
    )


def _status(config: Config, slug: str | None) -> int:
    slugs = [slug] if slug else _state_slugs(config)
    for name in slugs:
        state = StateStore(config.state_dir, name).load()
        skipped = sum(1 for item in state.history if item.get("status") == "superseded")
        print(
            f"{name}: last={state.last_export_id} "
            f"applied={len(state.history) - skipped} superseded={skipped}"
        )
    return 0


def _slug_of(arguments: argparse.Namespace) -> str | None:
    """Repository slug the current command works on, if it targets exactly one."""
    slug = getattr(arguments, "slug", None)
    if slug:
        return str(slug)
    export = getattr(arguments, "export", None)
    if export is not None:
        return Path(os.path.abspath(export)).parent.name
    return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    configure_logging(arguments.log_level, arguments.log_format)
    progress.configure(arguments.progress_interval)
    try:
        location = arguments.config or os.environ.get(CONFIG_ENV)
        if not location:
            raise ConfigError(f"no config file: use --config or set {CONFIG_ENV}")
        config = load_config(Path(location))
        log_event(
            logging.DEBUG,
            "config loaded",
            command=arguments.command,
            config=str(location),
            version=buildinfo.describe(),
            export_dir=config.export_dir,
            mirror_root=config.mirror_root,
            state_dir=config.state_dir,
            staging_dir=config.staging_dir,
            keyring=config.keyring,
            require_signature=config.require_signature,
            events_dir=config.events_dir,
        )
        events.configure(config.events_dir, config.events_keep_days)
        if arguments.command == "status":
            return _status(config, arguments.slug)
        if arguments.command == "serve":
            from mirrorhub.importer import webui  # only the UI needs http.server

            try:
                return webui.serve(config, Path(location), arguments.listen)
            except (ValueError, OSError) as exc:
                raise ConfigError(f"cannot start the web UI: {exc}") from exc
        if arguments.command == "check":
            return _check(config, arguments)
        if arguments.command == "adopt":
            result = adopt(
                config,
                arguments.slug,
                export_id=arguments.export_id,
                hash_contents=arguments.hash,
            )
            check = "quick check and SHA-256" if result.hashed else "quick check"
            events.emit(
                "import_adopted",
                result.slug,
                export_id=result.export_id,
                export_mode=_mode_of(result.export_id),
            )
            _emit_tree_check(
                result.slug,
                result.export_id,
                "adopt",
                [],
                hashed=result.hashed,
                entries=result.entries,
            )
            print(
                f"adopted {result.export_id} for {result.slug}: "
                f"{result.entries} entries match ({check})"
            )
            _warn_permissions(config, result.slug, result.directories)
            return 0
        return _run_import_with_event(config, arguments)
    except ImporterError as exc:
        slug = _slug_of(arguments)
        extra: dict[str, object] = {"slug": slug} if slug else {}
        log_event(logging.ERROR, "failed", error=str(exc), kind=type(exc).__name__, **extra)
        if arguments.command == "import" and not getattr(arguments, "dry_run", False):
            _emit_failure("import_failed", slug, exc, export_id=None)
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
