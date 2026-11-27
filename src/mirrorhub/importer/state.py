"""Receiver state per repository slug, stored outside the mirror tree."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mirrorhub.importer.checks import SLUG_RE
from mirrorhub.importer.errors import ImporterError, LockError, VerificationFailed


@dataclass
class RepoState:
    """The receiver's state of one repository: last applied export and history."""

    slug: str
    last_export_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def applied(self, export_id: str) -> bool:
        """Return True if the export was applied (or adopted); skipped ones were not."""
        return any(
            item.get("export_id") == export_id and item.get("status") != "superseded"
            for item in self.history
        )

    def superseded(self, export_id: str) -> bool:
        """Return True if the export was recorded as superseded (skipped)."""
        return any(
            item.get("export_id") == export_id and item.get("status") == "superseded"
            for item in self.history
        )

    def is_superseded_by_state(self, export_id: str) -> bool:
        """Return True if an unapplied export is older than the last applied one.

        It was skipped by a later export with an earlier base (or a full export with
        --rebase). Export ids sort by time.
        """
        return (
            self.last_export_id is not None
            and export_id < self.last_export_id
            and not self.applied(export_id)
        )


class StateStore:
    """The state file, inventory copies and lock of one repository on the receiver."""

    def __init__(self, state_dir: Path, slug: str) -> None:
        if not SLUG_RE.fullmatch(slug):
            raise VerificationFailed(f"invalid repository slug: {slug!r}")
        self.state_dir = state_dir
        self.slug = slug
        self.path = state_dir / f"{slug}.json"
        self.lock_path = state_dir / f"{slug}.lock"

    def load(self) -> RepoState:
        """Read the state; an empty state if the file does not exist yet."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return RepoState(self.slug)
        except (OSError, ValueError) as exc:
            raise ImporterError(f"cannot read state {self.path}: {exc}") from exc
        history = raw.get("history") if isinstance(raw, dict) else None
        last = raw.get("last_export_id") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("format") != 1
            or raw.get("slug") != self.slug
            or not isinstance(history, list)
            or not (last is None or isinstance(last, str))
        ):
            raise ImporterError(f"state file is invalid: {self.path}")
        return RepoState(self.slug, last, history)

    @property
    def inventory_dir(self) -> Path:
        """Directory of the kept files.json copies of this repository."""
        return self.state_dir / "inventories" / self.slug

    def save_inventory(self, export_id: str, source: Path) -> Path:
        """Keep a copy of the files.json that describes the tree after export_id.

        `importer check` compares the tree with it, also after the export has left
        the inbox. Only the copy for the current state is kept.
        """
        self.inventory_dir.mkdir(parents=True, exist_ok=True)
        target = self.inventory_dir / f"{export_id}.json"
        temporary = self.inventory_dir / f".{export_id}.json.tmp"
        with source.open("rb") as reader, temporary.open("wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
        for other in self.inventory_dir.glob("*.json"):
            if other != target:
                other.unlink(missing_ok=True)
        return target

    def load_inventory(self, export_id: str) -> dict[str, Any] | None:
        """Return the kept files.json of an export, or None if there is no copy."""
        path = self.inventory_dir / f"{export_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ImporterError(f"cannot read inventory copy {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ImporterError(f"inventory copy is invalid: {path}")
        return value

    def save(self, state: RepoState) -> None:
        """Write the state atomically (temporary file, fsync, rename)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "format": 1,
            "slug": state.slug,
            "last_export_id": state.last_export_id,
            "history": state.history,
        }
        temporary = self.state_dir / f".{self.slug}.json.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the repository lock (flock) for the block; LockError if it is taken."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(f"another import holds the lock for {self.slug!r}") from exc
            yield
        finally:
            os.close(descriptor)
