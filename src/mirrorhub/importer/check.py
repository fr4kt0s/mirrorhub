"""Compare a mirror tree with the inventory of its last applied export.

    python -m mirrorhub.importer --config importer.toml check --slug ubuntu [--hash]

Finds files deleted, truncated, replaced or added by hand since the last import.
The quick check reads only file system metadata; --hash also reads every file and
finds silent corruption. Nothing is changed. Exit code 11 when the tree differs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mirrorhub.importer import tree
from mirrorhub.importer.checks import SLUG_RE, Inventory, load_metadata
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import ChainError, ConfigError, IoFailure
from mirrorhub.importer.logs import log_event
from mirrorhub.importer.state import StateStore


@dataclass
class CheckResult:
    """Result of ``check``: the compared export, entries and differences found."""

    slug: str
    export_id: str
    entries: int
    hashed: bool
    differences: list[str] = field(default_factory=list)
    directories: list[Path] = field(default_factory=list)  # walked by the quick check


def _inventory(config: Config, store: StateStore, export_id: str) -> Inventory:
    """Return the inventory of an applied export.

    From the local copy, or from the inbox for states written before copies were kept.
    """
    stored = store.load_inventory(export_id)
    if stored is not None:
        log_event(logging.DEBUG, "inventory from local copy", slug=store.slug, export_id=export_id)
        return stored
    directory = config.export_dir / store.slug / export_id
    if not (directory / ".done").is_file():
        raise ConfigError(
            f"no inventory of {export_id}: no local copy and the export is no longer "
            "in the inbox; the next import or adopt writes one"
        )
    log_event(logging.DEBUG, "inventory from the inbox", slug=store.slug, export_id=export_id)
    metadata = load_metadata(directory)
    store.save_inventory(export_id, directory / "files.json")
    return metadata.inventory


def check_tree(config: Config, slug: str, *, hash_contents: bool = False) -> CheckResult:
    """Compare the repository's tree with the last applied export's files.json.

    Holds the repository lock, so no import changes the tree meanwhile. Compares
    type, size, mtime and symlink targets; hash_contents (``check --hash``) also
    reads and hashes every file. Changes nothing.
    """
    if not SLUG_RE.fullmatch(slug):
        raise ConfigError(f"invalid slug: {slug!r}")
    store = StateStore(config.state_dir, slug)
    target = config.mirror_root / slug
    try:
        with store.locked():  # no import may change the tree meanwhile
            state = store.load()
            if state.last_export_id is None:
                raise ChainError(f"no export has been applied for {slug}; nothing to check")
            inventory = _inventory(config, store, state.last_export_id)
            export_id = state.last_export_id
            walked: list[Path] = []
            entries = tree.scan(
                target, inventory, slug=slug, export_id=export_id, directories=walked
            )
            found = tree.differences(inventory, entries)
            if hash_contents:
                reported = frozenset(line.partition(": ")[2] for line in found)
                found += tree.hash_contents(
                    inventory, target, reported, slug=slug, export_id=export_id
                )
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc
    return CheckResult(slug, state.last_export_id, len(inventory), hash_contents, found, walked)
