"""Environment-driven settings.

Runtime-tunable values can be overridden by the setting table.
"""

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"

    global_concurrency: int = Field(default=2, ge=1, le=64)
    heartbeat_interval_s: int = Field(default=10, ge=1)
    heartbeat_stale_s: int = Field(default=60, ge=5)
    log_retention_days: int = Field(default=30, ge=1)

    session_ttl_s: int = Field(default=8 * 3600, ge=300)
    login_max_attempts: int = Field(default=5, ge=1)
    login_lockout_s: int = Field(default=300, ge=10)

    max_gpg_upload_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "db" / "mirrorhub.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def manifest_dir(self) -> Path:
        return self.state_dir / "manifests"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    def ensure_dirs(self) -> None:
        for path in (
            self.db_path.parent,
            self.log_dir,
            self.manifest_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_secret_key(settings: Settings | None = None) -> str:
    """Return the configured secret or the persistent generated secret."""
    current = settings or get_settings()
    if current.secret_key:
        return current.secret_key

    current.state_dir.mkdir(parents=True, exist_ok=True)
    key_file = current.state_dir / "secret_key"

    if not key_file.exists():
        try:
            descriptor = os.open(
                key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(48))

    return key_file.read_text().strip()
