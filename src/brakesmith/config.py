from __future__ import annotations

import json
import os
import tempfile
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


def default_format_settings_path() -> Path:
    return default_config_path().with_name("format-settings.json")


def load_format_settings(path: Path | None = None) -> dict[str, object] | None:
    settings_path = (path or default_format_settings_path()).expanduser().resolve()
    if not settings_path.exists():
        return None
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrakeSmithError(f"Cannot read saved format settings {settings_path}: {error}") from error
    if not isinstance(payload, dict):
        raise BrakeSmithError(f"Invalid saved format settings: {settings_path}")
    return payload


def save_format_settings(settings: dict[str, object], path: Path | None = None) -> Path:
    settings_path = (path or default_format_settings_path()).expanduser().resolve()
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=settings_path.parent, delete=False
        ) as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(settings_path)
    except OSError as error:
        raise BrakeSmithError(f"Cannot save format settings {settings_path}: {error}") from error
    return settings_path


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
