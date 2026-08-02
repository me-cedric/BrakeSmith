from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.9-3.10
    import tomli as tomllib

from .core import BrakeSmithError


def default_config_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "brakesmith" / "config.toml"


def load_profile(name: str | None, path: Path | None = None) -> dict[str, object]:
    if not name:
        return {}
    config_path = (path or default_config_path()).expanduser().resolve()
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BrakeSmithError(f"Cannot read config {config_path}: {error}") from error
    profiles = payload.get("profiles", {})
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise BrakeSmithError(f"Profile '{name}' not found in {config_path}")
    return profile


def prefer_profile(
    profile: dict[str, object], key: str, current: object, cli_default: object
) -> object:
    return profile.get(key, current) if current == cli_default else current
