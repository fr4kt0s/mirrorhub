"""Which user runs the importer, and may it change the directories an import touches?

Issue #60: an import aborted mid-way with "Permission denied" while deleting a file in
a tree whose directories belonged to a foreign uid (copied from another system with
numeric ownership kept). Deleting or replacing a file needs write and search
permission on the directory that holds it; the file's own owner and mode do not
matter. The importer creates every file and directory as the user running it, so
such directories always come from outside.

- preflight(): before the tree is touched (and in a dry run), every directory a
  plan or journal changes must be writable and searchable for the running user;
  otherwise the import stops with the directories, their owners and modes, and a
  `chown` suggestion, and nothing has been changed.
- explain(): a PermissionError that still happens later becomes a message with the
  phase, the running user, the path and the owner and mode of its directory.
- unwritable_dirs(): directories of a tree the running user cannot change, for the
  warnings of `check` and `adopt`.
"""

from __future__ import annotations

import os
import posixpath
import pwd
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from mirrorhub.importer.checks import SKIP_DIRECTORIES
from mirrorhub.importer.errors import IoFailure

SHOWN = 10  # directories listed in a message
EXAMPLES = 3  # paths listed per directory


class PermissionFailure(IoFailure):
    """The running user may not change a directory the import needs (exit code 9)."""


def _user_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def who() -> str:
    """'user mirrorhub (uid 1001)' for the effective user."""
    uid = os.geteuid()
    name = _user_name(uid)
    return f"user {name} (uid {uid})" if name else f"uid {uid}"


def describe(path: Path) -> str:
    """Owner and mode of a path: 'owner uid 1000 (no user on this host), drwxr-xr-x'."""
    try:
        info = path.lstat()
    except OSError as exc:
        return f"cannot be inspected: {exc.strerror or exc}"
    name = _user_name(info.st_uid)
    owner = (
        f"owner {name} (uid {info.st_uid})"
        if name
        else (f"owner uid {info.st_uid} (no user on this host)")
    )
    return f"{owner}, {stat.filemode(info.st_mode)}"


def _effective() -> bool:
    return os.access in os.supports_effective_ids


def writable(directory: Path) -> bool:
    """May the running user create, delete and rename entries in directory?"""
    return os.access(directory, os.W_OK | os.X_OK, effective_ids=_effective())


def _existing(path: Path) -> Path:
    """path, or its nearest existing ancestor (where a missing directory is created)."""
    while not os.path.lexists(path) and path != path.parent:
        path = path.parent
    return path


@dataclass
class Problem:
    directory: Path
    actions: list[str] = field(default_factory=list)  # what the import needs to do there
    examples: list[str] = field(default_factory=list)
    count: int = 0

    @property
    def action(self) -> str:
        return ", ".join(self.actions)


def preflight(
    target: Path,
    staging_root: Path,
    deletes: Iterable[str],
    installs: Iterable[str],
) -> list[Problem]:
    """Directories the import must change but the running user cannot.

    target is <mirror_root>/<slug> (it may not exist yet), staging_root the
    configured staging_dir. installs are new or replaced paths, deletes removed
    ones, all relative to target. Each directory is checked once.
    """
    problems: dict[Path, Problem] = {}
    # per relative directory: (directory to check, must be created, allowed); one lstat
    # and one access() per directory, not per file (a million paths over NFS is slow)
    known: dict[str, tuple[Path, bool, bool]] = {}

    def add(directory: Path, action: str, relative: str | None) -> None:
        problem = problems.setdefault(directory, Problem(directory))
        if action not in problem.actions:
            problem.actions.append(action)
        problem.count += 1
        if relative is not None and len(problem.examples) < EXAMPLES:
            problem.examples.append(relative)

    def need(relative_dir: str, action: str, relative: str | None) -> None:
        entry = known.get(relative_dir)
        if entry is None:
            wanted = target / relative_dir
            existing = _existing(wanted)
            entry = known[relative_dir] = (existing, existing != wanted, writable(existing))
        directory, created, allowed = entry
        if not allowed:
            add(directory, "create directories" if created else action, relative)

    staging = _existing(staging_root)
    if not writable(staging):
        add(staging, "create the staging directory", None)
    if not os.path.lexists(target):
        need("", "create the repository directory", None)
    for relative in deletes:
        need(posixpath.dirname(relative), "delete files", relative)
    for relative in installs:
        need(posixpath.dirname(relative), "add or replace files", relative)
    return list(problems.values())


def report(problems: list[Problem], target: Path, *, changed: bool = False) -> str:
    """Message for PermissionFailure: what, where, owners, how to fix it."""
    total = sum(max(problem.count, 1) for problem in problems)
    lines = [
        f"permission check failed: {who()} cannot change {len(problems)} "
        f"director{'y' if len(problems) == 1 else 'ies'} this import needs "
        f"({total} path{'' if total == 1 else 's'})" + ("" if changed else "; nothing was changed"),
    ]
    for problem in problems[:SHOWN]:
        lines.append(f"  {problem.directory}: {describe(problem.directory)} — {problem.action}")
        for example in problem.examples:
            lines.append(f"      e.g. {example}")
    if len(problems) > SHOWN:
        lines.append(f"  … and {len(problems) - SHOWN} more directories")
    user = _user_name(os.geteuid()) or str(os.geteuid())
    lines.append(
        f"fix: make the importer's user own the tree, e.g. sudo chown -R {user}: {target} "
        "(and the staging directory), then run the import again; see docs/importer.md, "
        '"User and permissions"'
    )
    return "\n".join(lines)


def explain(exc: PermissionError, phase: str, hint: str = "") -> PermissionFailure:
    """A PermissionError with the phase, the running user and the directory's owner."""
    path = Path(exc.filename) if exc.filename else None
    lines = [f"permission denied while {phase}: {path or exc}", f"  running as {who()}"]
    if path is not None:
        directory = path.parent
        lines.append(f"  directory {directory}: {describe(directory)}")
        if os.path.lexists(path):
            lines.append(f"  entry {path}: {describe(path)}")
    if hint:
        lines.append(hint)
    lines.append('see docs/importer.md, "User and permissions"')
    return PermissionFailure("\n".join(lines))


def _walk(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for directory, names, _ in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name not in SKIP_DIRECTORIES]
        yield Path(directory)


def unwritable_dirs(
    root: Path, limit: int = SHOWN, directories: Iterable[Path] | None = None
) -> tuple[int, list[Path]]:
    """Directories below root (root included) the running user cannot change:
    their number and the first `limit` of them. Symlinks are not followed.

    `directories` are the directories a tree scan already walked; without them
    the tree is walked here."""
    count = 0
    found: list[Path] = []
    for path in _walk(root) if directories is None else directories:
        if not writable(path):
            count += 1
            if len(found) < limit:
                found.append(path)
    return count, found


def tree_warning(root: Path, directories: Iterable[Path] | None = None) -> str | None:
    """Warning text for check/adopt if directories of the tree are not changeable."""
    count, found = unwritable_dirs(root, directories=directories)
    if not count:
        return None
    lines = [
        f"warning: {who()} cannot change {count} director"
        f"{'y' if count == 1 else 'ies'} of {root}; an import that adds, replaces or "
        "deletes files there will stop at the permission check:"
    ]
    lines += [f"  {path}: {describe(path)}" for path in found]
    if count > len(found):
        lines.append(f"  … and {count - len(found)} more")
    user = _user_name(os.geteuid()) or str(os.geteuid())
    lines.append(f"fix: sudo chown -R {user}: {root}")
    return "\n".join(lines)
