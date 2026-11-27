"""Importer configuration loaded from a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mirrorhub.importer.errors import ConfigError

KNOWN_KEYS = frozenset(
    {
        "export_dir",
        "mirror_root",
        "state_dir",
        "staging_dir",
        "keyring",
        "require_signature",
        "events_dir",
        "events_keep_days",
        "reports_dir",
    }
)


@dataclass(frozen=True)
class Config:
    """Importer settings.

    export_dir is the inbox: EXPORT_DIR/<repo-slug>/<export-id>/.
    mirror_root holds one mirror tree per slug (mirror_root/<slug>).
    state_dir and the lock files must live outside the mirror tree.
    """

    export_dir: Path
    mirror_root: Path
    state_dir: Path
    staging_dir: Path
    keyring: Path | None = None
    require_signature: bool = False
    # Event files for monitoring (docs/checkmk.md); None = off.
    events_dir: Path | None = None
    events_keep_days: int = 30
    # Default for check --output-dir and import --drift-output; None = off.
    reports_dir: Path | None = None


def _path(base: Path, value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config key {key!r} must be a non-empty string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def load_config(path: Path) -> Config:
    """Read and check the importer's TOML configuration (ConfigError if invalid)."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc

    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for key in ("export_dir", "mirror_root", "state_dir"):
        if key not in raw:
            raise ConfigError(f"missing config key {key!r}")

    base = path.resolve().parent
    mirror_root = _path(base, raw["mirror_root"], "mirror_root")
    staging = raw.get("staging_dir")
    require = raw.get("require_signature", False)
    if not isinstance(require, bool):
        raise ConfigError("config key 'require_signature' must be a boolean")
    keyring = raw.get("keyring")
    events_dir = raw.get("events_dir")
    keep_days = raw.get("events_keep_days", 30)
    if not isinstance(keep_days, int) or isinstance(keep_days, bool) or keep_days < 0:
        raise ConfigError("config key 'events_keep_days' must be a non-negative integer")
    return Config(
        export_dir=_path(base, raw["export_dir"], "export_dir"),
        mirror_root=mirror_root,
        state_dir=_path(base, raw["state_dir"], "state_dir"),
        staging_dir=(
            _path(base, staging, "staging_dir") if staging else mirror_root / ".mirrorhub-import"
        ),
        keyring=_path(base, keyring, "keyring") if keyring else None,
        require_signature=require,
        events_dir=_path(base, events_dir, "events_dir") if events_dir else None,
        events_keep_days=keep_days,
        reports_dir=_path(base, raw["reports_dir"], "reports_dir")
        if raw.get("reports_dir")
        else None,
    )
