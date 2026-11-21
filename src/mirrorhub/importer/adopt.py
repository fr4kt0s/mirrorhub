"""Adopt an export as the receiver state when the state file is lost.

The tree is compared with the export's files.json. The default quick check reads only
file system metadata (type, size, nanosecond mtime, symlink target, no extra paths),
which is meaningful because the importer sets every mtime from files.json. --hash
also compares SHA-256 and reads every file once. Nothing is transferred or changed in
the tree; on an exact match only the state file is written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mirrorhub.importer import tree
from mirrorhub.importer.checks import (
    EXPORT_ID_RE,
    SLUG_RE,
    ExportMetadata,
    check_signature,
    load_metadata,
)
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import ChainError, ConfigError, IoFailure, PostCheckError
from mirrorhub.importer.logs import log_event
from mirrorhub.importer.state import StateStore


@dataclass(frozen=True)
class Adopted:
    slug: str
    export_id: str
    entries: int
    hashed: bool


def _candidates(config: Config, slug: str, export_id: str | None) -> list[Path]:
    directory = config.export_dir / slug
    if export_id is not None:
        if not EXPORT_ID_RE.fullmatch(export_id):
            raise ConfigError(f"invalid export id: {export_id!r}")
        candidate = directory / export_id
        if not (candidate / ".done").is_file() or candidate.is_symlink():
            raise ConfigError(f"export not found in the inbox: {candidate}")
        return [candidate]
    if not directory.is_dir():
        raise ConfigError(f"no inbox directory for {slug!r}: {directory}")
    published = [
        child
        for child in sorted(directory.iterdir())
        if EXPORT_ID_RE.fullmatch(child.name)
        and child.is_dir()
        and not child.is_symlink()
        and (child / ".done").is_file()
    ]
    if not published:
        raise ConfigError(f"no published exports in the inbox for {slug!r}")
    return list(reversed(published))  # newest first


def adopt(
    config: Config,
    slug: str,
    *,
    export_id: str | None = None,
    hash_contents: bool = False,
) -> Adopted:
    """Write the receiver state for the export the tree matches exactly.

    export_id None means: try the published exports in the inbox, newest first.
    Refused if a state already exists or an import is unfinished.
    """
    if not SLUG_RE.fullmatch(slug):
        raise ConfigError(f"invalid slug: {slug!r}")
    store = StateStore(config.state_dir, slug)
    target = config.mirror_root / slug
    try:
        with store.locked():
            state = store.load()
            if state.last_export_id is not None or state.history:
                raise ChainError(
                    f"receiver state for {slug} exists (last={state.last_export_id}); "
                    "adopt is only for a missing state file"
                )
            if (config.staging_dir / slug / "journal.json").exists():
                raise IoFailure(
                    "staging holds an unfinished import; finish it or pass "
                    "--discard-staging to the next import first"
                )
            if not target.is_dir():
                raise ConfigError(f"no mirror tree for {slug!r}: {target}")

            candidates = _candidates(config, slug, export_id)
            entries = tree.scan_tree(target)
            chosen: ExportMetadata | None = None
            first_failure = ""
            for candidate in candidates:
                metadata = load_metadata(candidate)
                check_signature(candidate, config.keyring, config.require_signature)
                found = tree.differences(metadata.inventory, entries)
                if not found:
                    chosen = metadata
                    break
                if not first_failure:
                    first_failure = tree.summary(metadata.export_id, found)
            if chosen is None:
                prefix = "" if export_id is not None else "no export in the inbox matches; "
                raise PostCheckError(prefix + first_failure)
            if hash_contents:
                found = tree.content_differences(chosen.inventory, target)
                if found:
                    raise PostCheckError(tree.summary(chosen.export_id, found))

            state.last_export_id = chosen.export_id
            state.history.append(
                {
                    "export_id": chosen.export_id,
                    "status": "adopted",
                    "mode": chosen.mode,
                    "base_export_id": chosen.base_export_id,
                    "adopted_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "hash_checked": hash_contents,
                    "entries": len(chosen.inventory),
                }
            )
            store.save_inventory(chosen.export_id, chosen.directory / "files.json")
            store.save(state)
    except OSError as exc:
        raise IoFailure(f"I/O error: {exc}") from exc

    log_event(
        logging.INFO,
        "adopted",
        slug=slug,
        export_id=chosen.export_id,
        hashed=hash_contents,
    )
    return Adopted(slug, chosen.export_id, len(chosen.inventory), hash_contents)
