from pathlib import Path

import pytest

from brakesmith.config import (
    load_format_settings,
    load_profile,
    prefer_profile,
    save_format_settings,
)
from brakesmith.core import BrakeSmithError


def test_named_profile_and_cli_precedence(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[profiles.archive]\nquality = 16\npreset = "slower"\n')
    profile = load_profile("archive", config)
    assert prefer_profile(profile, "quality", 18.0, 18.0) == 16
    assert prefer_profile(profile, "quality", 20.0, 18.0) == 20.0


def test_missing_profile_fails(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text("[profiles]\n")
    with pytest.raises(BrakeSmithError, match="not found"):
        load_profile("missing", config)


def test_format_settings_round_trip_and_replace(tmp_path: Path):
    path = tmp_path / "nested" / "format.json"
    save_format_settings({"format_preset": "high"}, path)
    save_format_settings({"format_preset": "compact"}, path)
    assert load_format_settings(path) == {"format_preset": "compact"}
