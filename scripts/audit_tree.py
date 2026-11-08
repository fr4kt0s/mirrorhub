#!/usr/bin/env python3
"""Compare a mirror tree with the files.json of an export (read-only).

Usage:
    python3 audit_tree.py EXPORT_DIR TREE_DIR
    python3 audit_tree.py -h

Arguments:
    EXPORT_DIR  Export directory that contains files.json, normally the last export
                that was applied to this repository, for example
                /srv/mirrorhub/inbox/ubuntu/20261107T185852Z-full
    TREE_DIR    Mirror tree of the same repository, for example
                /srv/mirrorhub/mirror/ubuntu

Example:
    python3 audit_tree.py /srv/mirrorhub/inbox/ubuntu/20261107T185852Z-full \
        /srv/mirrorhub/mirror/ubuntu

Output, one line per problem:
    MISSING  <path>   listed in files.json but not present in the tree
    DIFFERS  <path>   wrong type, size, sha256 or symlink target
    EXTRA    <path>   present in the tree but not listed in files.json
A summary line "N problem(s)" is printed last.

Notes:
    - Nothing is modified. Both directories are only read.
    - Every file is hashed, so large mirrors take a while.
    - Directories named .temp, .tmp, .mirrorhub and .rsync-partial are ignored,
      like on the sender. Only files and symlinks are compared, not directories.
    - mtimes and file modes are not compared.

Exit codes:
    0  the tree matches files.json
    1  at least one problem was found
    2  wrong arguments, or files.json / the tree could not be read
"""

import hashlib
import json
import os
import sys
from pathlib import Path

SKIP = {".temp", ".tmp", ".mirrorhub", ".rsync-partial"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(argv) != 3:
        print("usage: audit_tree.py EXPORT_DIR TREE_DIR  (see -h)", file=sys.stderr)
        return 2
    export, tree = Path(argv[1]), Path(argv[2])
    if not tree.is_dir():
        print(f"error: tree directory not found: {tree}", file=sys.stderr)
        return 2
    try:
        inventory = json.loads((export / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {export / 'files.json'}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(inventory, dict):
        print("error: files.json is not an inventory", file=sys.stderr)
        return 2

    problems = 0
    for rel, entry in sorted(inventory.items()):
        path = tree / rel
        if not os.path.lexists(path):
            print("MISSING ", rel)
            problems += 1
        elif entry["type"] == "symlink":
            if not (path.is_symlink() and os.readlink(path) == entry["target"]):
                print("DIFFERS ", rel)
                problems += 1
        elif path.is_symlink() or not path.is_file():
            print("DIFFERS ", rel)
            problems += 1
        elif path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            print("DIFFERS ", rel)
            problems += 1

    present = set()
    for directory, dirs, files in os.walk(tree):
        base = Path(directory)
        links = [n for n in dirs if (base / n).is_symlink()]
        dirs[:] = [n for n in dirs if n not in SKIP and n not in links]
        for name in [*files, *links]:
            present.add((base / name).relative_to(tree).as_posix())
    for rel in sorted(present - set(inventory)):
        print("EXTRA   ", rel)
        problems += 1

    print(f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))