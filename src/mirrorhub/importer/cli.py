"""Command line interface: python -m mirrorhub.importer."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mirrorhub import buildinfo
from mirrorhub.importer import tree
from mirrorhub.importer.adopt import adopt
from mirrorhub.importer.apply import Options, Outcome, import_export, skip_jumped
from mirrorhub.importer.check import check_tree
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config, load_config
from mirrorhub.importer.errors import ConfigError, DriftError, ImporterError
from mirrorhub.importer.logs import configure_logging, log_event
from mirrorhub.importer.state import StateStore

CONFIG_ENV = "MIRRORHUB_IMPORTER_CONFIG"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirrorhub.importer", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {buildinfo.describe()}")
    parser.add_argument("--config", type=Path, help=f"TOML file (or set {CONFIG_ENV})")
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("import", help="apply exports from the configured inbox")
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
    run.add_argument("--full-check", action="store_true", help="re-hash the whole tree afterwards")
    run.add_argument(
        "--no-tree-check",
        action="store_true",
        help="skip the quick comparison of the whole tree with files.json after importing",
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

    status = commands.add_parser("status", help="show receiver state")
    status.add_argument("--slug", help="repository slug (default: all)")

    check = commands.add_parser(
        "check", help="compare the tree with the inventory of its last applied export"
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
    if outcome.drift:
        print(
            f"warning: the tree differs from files.json of {outcome.export_id} "
            f"in {len(outcome.drift)} places (not caused by this export):"
        )
        _print_differences(outcome.drift)


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


def _report(directory: Path | None, slug: str, outcome: Outcome) -> None:
    if directory is None or not outcome.tree_checked:
        return
    written = tree.write_report(directory, slug, outcome.export_id, outcome.drift, hashed=False)
    print(f"differences written to {written}")


def _import_slug(config: Config, slug: str, options: Options, report: Path | None = None) -> bool:
    """Apply all pending exports of one repository; True if the tree differs afterwards.

    Exports are taken oldest first. One is skipped as superseded when it is older than
    the last applied export, or when a later pending export starts at the current state
    (an "incremental since" export that replaces it).
    """
    store = StateStore(config.state_dir, slug)
    done = 0
    drift = False
    pending = pending_exports(config, slug)
    for path in pending:
        state = store.load()
        if state.applied(path.name) or state.superseded(path.name):
            continue
        jumped_by = _jumped_by(path, pending, state.last_export_id)
        if jumped_by is not None:
            outcome = skip_jumped(path, config, options, jumped_by)
        else:
            outcome = import_export(path, config, options)
        _print(outcome, options.dry_run)
        _report(report, slug, outcome)
        done += 1
        drift = drift or bool(outcome.drift)
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
    return drift


def _import_all(config: Config, options: Options, report: Path | None = None) -> int:
    """Process every repository in the inbox; one failing slug does not stop the others."""
    if options.rebase or options.discard_staging:
        raise ConfigError("--rebase and --discard-staging cannot be used with --all")
    slugs = list_slugs(config)
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
    )
    report = arguments.drift_output
    if arguments.export is not None:
        outcome = import_export(arguments.export, config, options)
        _print(outcome, options.dry_run)
        _report(report, Path(os.path.abspath(arguments.export)).parent.name, outcome)
        return DriftError.exit_code if outcome.drift else 0
    if arguments.all_slugs:
        return _import_all(config, options, report)
    drift = _import_slug(config, arguments.slug, options, report)
    return DriftError.exit_code if drift else 0


def _state_slugs(config: Config) -> list[str]:
    return sorted(path.stem for path in config.state_dir.glob("*.json"))


def _check(config: Config, arguments: argparse.Namespace) -> int:
    """Exit 0 if every checked tree matches, 11 if one differs, else the worst error."""
    slugs = _state_slugs(config) if arguments.all_slugs else [arguments.slug]
    if not slugs:
        print("no repositories with a receiver state")
    worst = 0
    for slug in slugs:
        try:
            result = check_tree(config, slug, hash_contents=arguments.hash)
        except ImporterError as exc:
            if not arguments.all_slugs:
                raise
            print(f"{slug}: error: {exc}", file=sys.stderr)
            worst = max(worst, exc.exit_code)
            continue
        kind = "quick check and SHA-256" if result.hashed else "quick check"
        if arguments.output_dir is not None:
            written = tree.write_report(
                arguments.output_dir,
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
    configure_logging(arguments.log_level)
    try:
        location = arguments.config or os.environ.get(CONFIG_ENV)
        if not location:
            raise ConfigError(f"no config file: use --config or set {CONFIG_ENV}")
        config = load_config(Path(location))
        if arguments.command == "status":
            return _status(config, arguments.slug)
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
            print(
                f"adopted {result.export_id} for {result.slug}: "
                f"{result.entries} entries match ({check})"
            )
            return 0
        return _run_import(config, arguments)
    except ImporterError as exc:
        slug = _slug_of(arguments)
        extra: dict[str, object] = {"slug": slug} if slug else {}
        log_event(logging.ERROR, "failed", error=str(exc), kind=type(exc).__name__, **extra)
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
