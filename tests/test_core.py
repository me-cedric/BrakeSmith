import json
from pathlib import Path

import pytest

from brakesmith.core import (
    BrakeSmithError,
    MediaFile,
    Track,
    atomic_write_json,
    discover,
    ensure_source_unchanged,
    handbrake_command,
    normalize_languages,
    output_path,
    quarantine_file,
    select_tracks,
    snapshot_source,
    validate_destinations,
    validate_output,
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


def test_discover_accepts_custom_extension(tmp_path: Path):
    unusual = tmp_path / "archive.video"
    unusual.touch()
    assert discover(tmp_path, extra_extensions=["video"]) == [unusual]


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


def test_command_can_keep_reconciled_unknown_tracks(tmp_path: Path):
    media = MediaFile(
        tmp_path / "a.mp4",
        "h264",
        10,
        1,
        [Track(1, 1, "audio", "und")],
        [Track(2, 1, "subtitle", "und")],
    )
    command = handbrake_command(
        "HandBrakeCLI",
        media,
        tmp_path / "out.part",
        ["eng"],
        ["fra"],
        None,
        18,
        "slow",
        extra_audio=[1],
        extra_subtitles=[1],
    )
    assert command[command.index("--audio") + 1] == "1"
    assert command[command.index("--subtitle") + 1] == "1"


def test_source_snapshot_detects_change(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"before")
    snapshot = snapshot_source(source)
    source.write_bytes(b"after-change")
    with pytest.raises(BrakeSmithError, match="Source changed"):
        ensure_source_unchanged(snapshot)


def test_destination_collision_is_rejected(tmp_path: Path):
    destination = tmp_path / "movie.brakesmith.mkv"
    with pytest.raises(BrakeSmithError, match="Output collision"):
        validate_destinations(
            [(tmp_path / "movie.mp4", destination), (tmp_path / "movie.avi", destination)]
        )


def test_validate_output_checks_tracks(tmp_path: Path, monkeypatch):
    output = tmp_path / "movie.part"
    output.write_bytes(b"x" * 2048)
    monkeypatch.setattr(
        "brakesmith.core.probe",
        lambda *args: MediaFile(output, "hevc", 100, 2048, [Track(1, 1, "audio", "eng")]),
    )
    assert validate_output(output, "ffprobe", 100, 1, 0).codec == "hevc"
    with pytest.raises(BrakeSmithError, match="audio tracks"):
        validate_output(output, "ffprobe", 100, 2, 0)


def test_quarantine_never_overwrites(tmp_path: Path):
    partial = tmp_path / "movie.part"
    partial.write_text("first")
    (tmp_path / "movie.part.invalid").write_text("existing")
    quarantined = quarantine_file(partial)
    assert quarantined.name == "movie.part.invalid.1"
    assert quarantined.read_text() == "first"


def test_atomic_json_refuses_existing_report(tmp_path: Path):
    report = tmp_path / "summary.json"
    atomic_write_json(report, {"status": "ok"})
    assert json.loads(report.read_text()) == {"status": "ok"}
    with pytest.raises(BrakeSmithError, match="Report exists"):
        atomic_write_json(report, {"status": "changed"})
