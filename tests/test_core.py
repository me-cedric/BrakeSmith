from pathlib import Path

import pytest

from brakesmith.core import (
    BrakeSmithError,
    MediaFile,
    Track,
    discover,
    handbrake_command,
    normalize_languages,
    output_path,
    select_tracks,
)


def test_normalize_languages_aliases_and_deduplicates():
    assert normalize_languages(["French", "fre", "EN", "eng"]) == ["fra", "eng"]


def test_discover_respects_depth_and_ignores_outputs(tmp_path: Path):
    (tmp_path / "top.mp4").touch()
    (tmp_path / "top.brakesmith.mkv").touch()
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "deep.avi").touch()
    assert discover(tmp_path, 0) == [tmp_path / "top.mp4"]
    assert discover(tmp_path, 1) == [tmp_path / "top.mp4"]
    assert len(discover(tmp_path)) == 2


def test_discover_rejects_file(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.touch()
    with pytest.raises(BrakeSmithError):
        discover(path)


def test_track_selection_adds_original():
    tracks = [Track(1, 1, "audio", "eng"), Track(2, 2, "audio", "jpn"), Track(3, 3, "audio", "fra")]
    assert select_tracks(tracks, ["eng"], "jpn") == [1, 2]


def test_output_paths():
    source = Path("/media/show/episode.mp4")
    assert output_path(source) == Path("/media/show/episode.brakesmith.mkv")
    assert output_path(source, Path("/out"), Path("/media")) == Path(
        "/out/show/episode.brakesmith.mkv"
    )


def test_command_uses_copy_audio_and_selected_tracks(tmp_path: Path):
    media = MediaFile(
        tmp_path / "a.mp4",
        "h264",
        10,
        1,
        [Track(1, 1, "audio", "eng")],
        [Track(2, 1, "subtitle", "fra")],
    )
    command = handbrake_command(
        "HandBrakeCLI", media, tmp_path / "out.part", ["eng"], ["fra"], None, 18, "slow"
    )
    assert [command[command.index("--audio") + 1], command[command.index("--subtitle") + 1]] == [
        "1",
        "1",
    ]
    assert "x265_10bit" in command
