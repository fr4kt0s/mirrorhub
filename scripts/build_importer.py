"""Build the receiver-side importer as one runnable file: mirrorhub-importer.pyz.

The archive holds only what the importer needs (mirrorhub/__init__.py,
mirrorhub/verify.py, mirrorhub/importer/*.py) and runs with the Python standard
library alone, so it can be carried through the data diode as a single file:

    python3 scripts/build_importer.py [--output dist/mirrorhub-importer.pyz]
    python3 mirrorhub-importer.pyz --config importer.toml import --slug ubuntu

The build is reproducible: fixed timestamps and sorted entries give the same bytes
for the same sources and Python version. A .sha256 file is written next to it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "mirrorhub"
DEFAULT_OUTPUT = ROOT / "dist" / "mirrorhub-importer.pyz"
# 2020-01-01T00:00:00Z; zip timestamps cannot be earlier than 1980.
FIXED_MTIME = 1_577_836_800

ENTRY_POINT = '''"""Entry point of mirrorhub-importer.pyz."""

import sys

if sys.version_info < (3, 12):
    sys.exit("mirrorhub-importer needs Python 3.12 or newer, found " + sys.version.split()[0])

from mirrorhub.importer.cli import main

sys.exit(main())
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output: Path) -> Path:
    """Write the archive and its .sha256 file; returns the archive path."""
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / "stage"
        package = stage / "mirrorhub"
        (package / "importer").mkdir(parents=True)
        shutil.copyfile(SOURCE / "__init__.py", package / "__init__.py")
        shutil.copyfile(SOURCE / "verify.py", package / "verify.py")
        for path in sorted((SOURCE / "importer").glob("*.py")):
            shutil.copyfile(path, package / "importer" / path.name)
        (stage / "__main__.py").write_text(ENTRY_POINT, encoding="utf-8")

        for path in [*stage.rglob("*"), stage]:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
            os.utime(path, (FIXED_MTIME, FIXED_MTIME))

        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(f".{output.name}.tmp")
        zipapp.create_archive(
            stage,
            partial,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
        os.chmod(partial, 0o755)  # noqa: S103 - the archive is meant to be executable
        os.replace(partial, output)

    output.with_name(f"{output.name}.sha256").write_text(
        f"{_sha256(output)}  {output.name}\n", encoding="ascii"
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="archive path")
    arguments = parser.parse_args(argv)
    output = build(arguments.output.resolve())
    print(f"built {output} ({output.stat().st_size} bytes)")
    print(output.with_name(f"{output.name}.sha256").read_text(encoding="ascii").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
