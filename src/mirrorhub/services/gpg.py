"""Manage public keys in a dedicated GNUPGHOME.

Uploaded material is imported into a temporary home first. Only public keys are
exported from that home into trustedkeys.gpg.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, col, select

from mirrorhub.models import GpgKey
from mirrorhub.settings import get_settings

KEYRING_NAME = "trustedkeys.gpg"
KEYRING_DIR = Path("/usr/share/keyrings")


class GpgError(RuntimeError):
    """Keyring operation failed."""


@dataclass(frozen=True)
class KeyInfo:
    fingerprint: str
    uid: str


def keyring_path() -> Path:
    return get_settings().gnupg_dir / KEYRING_NAME


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GNUPGHOME"] = str(home)
    env["LC_ALL"] = "C"
    env["HOME"] = str(home)
    return env


def _run(
    args: list[str],
    *,
    home: Path,
    input_bytes: bytes | None = None,
    timeout: int = 60,
) -> bytes:
    """Execute GPG without a shell or system-keyring access."""
    executable = shutil.which("gpg")
    if executable is None:
        raise GpgError("gpg is not installed in this image")

    try:
        completed = subprocess.run(  # noqa: S603
            [executable, "--no-options", *args],
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            env=_env(home),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GpgError("gpg timed out") from exc
    except OSError as exc:
        raise GpgError("could not execute gpg") from exc

    if completed.returncode != 0:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = lines[-1][:300] if lines else f"gpg exited {completed.returncode}"
        raise GpgError(detail)

    return completed.stdout


def _parse_colons(output: str) -> list[KeyInfo]:
    keys: list[KeyInfo] = []
    expecting_primary_fingerprint = False

    for line in output.splitlines():
        fields = line.split(":")
        if not fields:
            continue

        record = fields[0]
        if record == "pub":
            expecting_primary_fingerprint = True
        elif record == "sub":
            expecting_primary_fingerprint = False
        elif record == "fpr" and len(fields) > 9 and expecting_primary_fingerprint:
            keys.append(KeyInfo(fingerprint=fields[9], uid=""))
            expecting_primary_fingerprint = False
        elif record == "uid" and len(fields) > 9 and keys and not keys[-1].uid:
            uid = fields[9].replace("\\x3a", ":")
            keys[-1] = KeyInfo(fingerprint=keys[-1].fingerprint, uid=uid)

    return keys


def keyring_args() -> list[str]:
    return [
        "--no-default-keyring",
        "--keyring",
        str(keyring_path()),
    ]


def list_keys() -> list[KeyInfo]:
    if not keyring_path().exists():
        return []

    output = _run(
        [*keyring_args(), "--with-colons", "--list-keys"],
        home=get_settings().gnupg_dir,
    )
    return _parse_colons(output.decode("utf-8", "replace"))


def import_key_bytes(data: bytes) -> list[KeyInfo]:
    if not data:
        raise GpgError("empty upload")

    settings = get_settings()
    if len(data) > settings.max_gpg_upload_bytes:
        raise GpgError("key material exceeds the size limit")

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.gnupg_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="gpg-import-",
        dir=settings.tmp_dir,
    ) as directory:
        scratch = Path(directory)
        os.chmod(scratch, 0o700)

        _run(
            ["--batch", "--no-tty", "--import"],
            home=scratch,
            input_bytes=data,
        )
        output = _run(
            ["--batch", "--with-colons", "--list-keys"],
            home=scratch,
        )
        imported = _parse_colons(output.decode("utf-8", "replace"))

        if not imported:
            raise GpgError("no public keys found in the upload")

        fingerprints = [key.fingerprint for key in imported]
        public = _run(
            ["--batch", "--export", *fingerprints],
            home=scratch,
        )
        if not public:
            raise GpgError("public key export was empty")

        _run(
            [*keyring_args(), "--batch", "--no-tty", "--import"],
            home=settings.gnupg_dir,
            input_bytes=public,
        )

    return imported


def import_key_file(path: Path) -> list[KeyInfo]:
    if not path.is_file():
        raise GpgError(f"keyring file not found: {path.name}")
    return import_key_bytes(path.read_bytes())


def delete_key(fingerprint: str) -> None:
    valid_lengths = {40, 64}
    if len(fingerprint) not in valid_lengths or any(
        character not in "0123456789abcdefABCDEF" for character in fingerprint
    ):
        raise GpgError("invalid primary-key fingerprint")

    _run(
        [*keyring_args(), "--batch", "--yes", "--delete-keys", fingerprint],
        home=get_settings().gnupg_dir,
    )


def package_candidates() -> list[Path]:
    if not KEYRING_DIR.is_dir():
        return []

    return sorted(
        path
        for path in KEYRING_DIR.glob("*.gpg")
        if "archive-keyring" in path.name or "keys" in path.name
    )


def sync_rows(db: Session) -> list[GpgKey]:
    """Reconcile database rows against the public keyring."""
    present = {key.fingerprint: key for key in list_keys()}
    known = {row.fingerprint: row for row in db.exec(select(GpgKey)).all()}

    for fingerprint, info in present.items():
        row = known.get(fingerprint)
        if row is None:
            db.add(
                GpgKey(
                    fingerprint=fingerprint,
                    uid=info.uid[:200],
                    source="upload",
                    keyring_name=KEYRING_NAME,
                )
            )
        elif info.uid and row.uid != info.uid:
            row.uid = info.uid[:200]
            db.add(row)

    for fingerprint, row in known.items():
        if fingerprint not in present:
            db.delete(row)

    db.commit()
    return list(db.exec(select(GpgKey).order_by(col(GpgKey.fingerprint))).all())
