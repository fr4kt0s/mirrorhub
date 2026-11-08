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
    slug: str
    last_export_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def applied(self, export_id: str) -> bool:
        return any(item.get("export_id") == export_id for item in self.history)


class StateStore:
    def __init__(self, state_dir: Path, slug: str) -> None:
        if not SLUG_RE.fullmatch(slug):
            raise VerificationFailed(f"invalid repository slug: {slug!r}")
        self.state_dir = state_dir
        self.slug = slug
        self.path = state_dir / f"{slug}.json"
        self.lock_path = state_dir / f"{slug}.lock"

    def load(self) -> RepoState:
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

    def save(self, state: RepoState) -> None:
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
