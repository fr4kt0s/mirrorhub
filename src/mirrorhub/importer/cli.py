"""Command line interface: python -m mirrorhub.importer."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mirrorhub.importer.apply import Options, Outcome, import_export
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config, load_config
from mirrorhub.importer.errors import ConfigError, ImporterError
from mirrorhub.importer.logs import configure_logging, log_event
from mirrorhub.importer.state import StateStore

CONFIG_ENV = "MIRRORHUB_IMPORTER_CONFIG"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirrorhub.importer", description=__doc__)
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
        "--discard-staging", action="store_true", help="drop another unfinished import"
    )

    status = commands.add_parser("status", help="show receiver state")
    status.add_argument("--slug", help="repository slug (default: all)")
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
        if not SLUG_RE.fullmatch(child.name):
            log_event(
                logging.WARNING,
                "skipping inbox directory, not a valid slug",
                name=child.name,
            )
            continue
        slugs.append(child.name)
    return slugs


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


def _import_slug(config: Config, slug: str, options: Options) -> int:
    """Apply all pending exports of one repository; returns how many were processed."""
    store = StateStore(config.state_dir, slug)
    done = 0
    for path in pending_exports(config, slug):
        state = store.load()
        if state.applied(path.name):
            continue
        if state.last_export_id is not None and path.name < state.last_export_id:
            log_event(
                logging.WARNING,
                "skipping older unapplied export",
                slug=slug,
                export_id=path.name,
            )
            continue
        _print(import_export(path, config, options), options.dry_run)
        done += 1
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
    return done


def _import_all(config: Config, options: Options) -> int:
    """Process every repository in the inbox; one failing slug does not stop the others."""
    if options.rebase or options.discard_staging:
        raise ConfigError("--rebase and --discard-staging cannot be used with --all")
    slugs = list_slugs(config)
    if not slugs:
        print("no repositories in the inbox")
    worst = 0
    for slug in slugs:
        try:
            _import_slug(config, slug, options)
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
    )
    if arguments.export is not None:
        _print(import_export(arguments.export, config, options), options.dry_run)
        return 0
    if arguments.all_slugs:
        return _import_all(config, options)
    _import_slug(config, arguments.slug, options)
    return 0


def _status(config: Config, slug: str | None) -> int:
    slugs = [slug] if slug else sorted(p.stem for p in config.state_dir.glob("*.json"))
    for name in slugs:
        state = StateStore(config.state_dir, name).load()
        print(f"{name}: last={state.last_export_id} applied={len(state.history)}")
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
        return _run_import(config, arguments)
    except ImporterError as exc:
        slug = _slug_of(arguments)
        extra: dict[str, object] = {"slug": slug} if slug else {}
        log_event(logging.ERROR, "failed", error=str(exc), kind=type(exc).__name__, **extra)
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
