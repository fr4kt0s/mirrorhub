"""Environment-driven settings.

Runtime-tunable values can be overridden by the setting table.
"""

import contextlib
import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration from ``MIRRORHUB_*`` environment variables (see ``.env.example``)."""

    model_config = SettingsConfigDict(env_prefix="MIRRORHUB_", extra="ignore")

    role: Literal["web", "worker", "cli"] = "cli"
    tool: str = ""

    state_dir: Path = Path("/var/lib/mirrorhub")
    data_dir: Path = Path("/data")
    export_dir: Path = Path("/export")
    gnupg_dir: Path = Path("/gnupg")

    admin_username: str = "admin"
    admin_password: str = ""
    secret_key: str = ""
    # Exact origin of the web interface (https://host:port), used for CSRF checks.
    public_origin: str = ""

    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"

    global_concurrency: int = Field(default=2, ge=1, le=64)
    heartbeat_interval_s: int = Field(default=10, ge=1)
    heartbeat_stale_s: int = Field(default=60, ge=5)
    log_retention_days: int = Field(default=30, ge=1)
    # Metadata copies kept per repository on the state volume; each one can serve as
    # the base of an "incremental since" export.
    export_meta_keep: int = Field(default=30, ge=1, le=10000)
    # Threads that stat and hash files while an inventory is built (export, post-check,
    # sync inventory before "export after sync"). Threads inside one process, not
    # containers. On NFS every stat and open is a network round trip, so several in
    # flight help; on a local disk 1-4 is enough. MIRRORHUB_EXPORT_WORKERS is the old
    # name (#64, 2026-11-26) and is still read, with a warning.
    inventory_threads: int = Field(
        default=8,
        ge=1,
        le=64,
        validation_alias=AliasChoices(
            "MIRRORHUB_INVENTORY_THREADS", "MIRRORHUB_EXPORT_WORKERS", "inventory_threads"
        ),
    )

    session_ttl_s: int = Field(default=8 * 3600, ge=300)
    login_max_attempts: int = Field(default=5, ge=1)
    login_lockout_s: int = Field(default=300, ge=10)

    max_gpg_upload_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def _warn_old_names(self) -> "Settings":
        if "MIRRORHUB_EXPORT_WORKERS" in os.environ:
            if "MIRRORHUB_INVENTORY_THREADS" in os.environ:
                note = "ignored, MIRRORHUB_INVENTORY_THREADS is set"
            else:
                note = "used for now"
            logging.getLogger(__name__).warning(
                "MIRRORHUB_EXPORT_WORKERS is deprecated (%s); rename it to "
                "MIRRORHUB_INVENTORY_THREADS",
                note,
            )
        return self

    @property
    def db_path(self) -> Path:
        """SQLite database file on the state volume."""
        return self.state_dir / "db" / "mirrorhub.sqlite3"

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL of the database file."""
        return f"sqlite:///{self.db_path}"

    @property
    def log_dir(self) -> Path:
        """Directory of the job logs."""
        return self.state_dir / "logs"

    @property
    def manifest_dir(self) -> Path:
        """Directory of hash caches, sync inventories and kept repair requests."""
        return self.state_dir / "manifests"

    @property
    def tmp_dir(self) -> Path:
        """Scratch directory on the state volume."""
        return self.state_dir / "tmp"

    def ensure_dirs(self) -> None:
        """Create the state directories that must exist before the first use."""
        for path in (
            self.db_path.parent,
            self.log_dir,
            self.manifest_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings, read once per process."""
    return Settings()


def get_secret_key(settings: Settings | None = None) -> str:
    """Return the configured secret or the persistent generated secret.

    The generated key is written to a temporary file and then hard-linked into place,
    so a concurrent caller sees either no file or the complete key, never an empty one.
    """
    current = settings or get_settings()
    if current.secret_key:
        return current.secret_key

    current.state_dir.mkdir(parents=True, exist_ok=True)
    key_file = current.state_dir / "secret_key"

    if not key_file.exists():
        temporary = current.state_dir / f".secret_key.{os.getpid()}.{secrets.token_hex(4)}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.flush()
                os.fsync(handle.fileno())
            # FileExistsError: another process won the race; use its key.
            with contextlib.suppress(FileExistsError):
                os.link(temporary, key_file)
        finally:
            temporary.unlink(missing_ok=True)

    key = key_file.read_text().strip()
    if not key:
        raise RuntimeError(f"{key_file} is empty; delete it or set MIRRORHUB_SECRET_KEY")
    return key
