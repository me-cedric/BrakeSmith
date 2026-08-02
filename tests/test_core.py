import json
import subprocess
from pathlib import Path

import pytest

from brakesmith.core import (
    BrakeSmithError,
    MediaFile,
    ProbeCache,
    Track,
    atomic_write_json,
    content_resolution,
    discover,
    ensure_source_unchanged,
    expected_audio_track_count,
    handbrake_command,
    normalize_languages,
    output_path,
    probe,
    quarantine_file,
    replacement_output_path,
    resolve_format_settings,
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


def test_discover_reports_live_counts(tmp_path: Path):
    (tmp_path / "show").mkdir()
    (tmp_path / "movie.mkv").touch()
    (tmp_path / "show" / "episode.mp4").touch()
    updates = []

    discover(tmp_path, on_progress=lambda directories, files: updates.append((directories, files)))

    assert updates[-1] == (2, 2)


def test_discover_rejects_file(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.touch()
    with pytest.raises(BrakeSmithError):
        discover(path)


def test_probe_ignores_cover_art_and_reports_ffprobe_reason(tmp_path: Path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"video")
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            },
            {"index": 1, "codec_type": "video", "codec_name": "h264"},
        ],
        "format": {"duration": "10"},
    }
    monkeypatch.setattr(
        "brakesmith.core.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
    )
    media = probe(source)
    assert media.codec == "h264"
    assert media.attachments == 1

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr="moov atom not found")

    monkeypatch.setattr("brakesmith.core.subprocess.run", fail)
    with pytest.raises(BrakeSmithError, match="moov atom not found"):
        probe(source)


def test_track_selection_adds_original():
    tracks = [Track(1, 1, "audio", "eng"), Track(2, 2, "audio", "jpn"), Track(3, 3, "audio", "fra")]
    assert select_tracks(tracks, ["eng"], "jpn") == [1, 2]


def test_track_selection_falls_back_to_defaults_and_first_audio():
    audio = [
        Track(1, 1, "audio", "jpn"),
        Track(2, 2, "audio", "deu", default=True),
    ]
    subtitles = [
        Track(3, 1, "subtitle", "jpn"),
        Track(4, 2, "subtitle", "deu", default=True),
    ]
    assert select_tracks(audio, ["eng"], fallback_default=True) == [2]
    assert select_tracks(subtitles, ["eng"], fallback_default=True) == [2]
    assert select_tracks(audio[:1], ["eng"], fallback_default=True) == [1]
    assert select_tracks(subtitles[:1], ["eng"], fallback_default=True) == []


def test_output_paths():
    source = Path("/media/show/episode.mp4")
    assert output_path(source) == Path("/media/show/episode.brakesmith.mkv")
    assert output_path(source, Path("/out"), Path("/media")) == Path(
        "/out/show/episode.brakesmith.mkv"
    )
    assert replacement_output_path(Path("/media/Movie.1080p.x264.mp4")) == Path(
        "/media/Movie.1080p.x265.mkv"
    )
    assert replacement_output_path(Path("/media/Movie AVC.avi")) == Path("/media/Movie HEVC.mkv")
    assert replacement_output_path(source) == Path("/media/show/episode.x265.mkv")


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
    with pytest.raises(BrakeSmithError, match="another source"):
        validate_destinations(
            [
                (tmp_path / "movie.x264.mkv", tmp_path / "movie.x265.mkv"),
                (tmp_path / "movie.x265.mkv", tmp_path / "other.mkv"),
            ]
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


def test_validate_output_allows_handbrake_preserved_attachments(tmp_path: Path, monkeypatch):
    output = tmp_path / "movie.part"
    output.write_bytes(b"x" * 2048)
    monkeypatch.setattr(
        "brakesmith.core.probe",
        lambda *args: MediaFile(
            output,
            "hevc",
            100,
            2048,
            [Track(1, 1, "audio", "eng")],
            attachments=4,
        ),
    )
    assert validate_output(output, "ffprobe", 100, 1, 0).attachments == 4


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


def test_probe_cache_invalidates_changed_source(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"first")
    cache_path = tmp_path / "cache.json"
    cache = ProbeCache(cache_path)
    cache.put(MediaFile(source, "h264", 10, source.stat().st_size))
    cache.save()
    assert ProbeCache(cache_path).get(source).codec == "h264"
    source.write_bytes(b"changed-size")
    assert ProbeCache(cache_path).get(source) is None


def test_track_flags_and_title_filters():
    tracks = [
        Track(1, 1, "audio", "eng", title="Main"),
        Track(2, 2, "audio", "eng", title="Director Commentary", commentary=True),
        Track(3, 3, "audio", "fra", title="Description", visual_impaired=True),
    ]
    assert select_tracks(
        tracks,
        ["eng", "fra"],
        keep_commentary=False,
        exclude_titles=["description"],
    ) == [1]


def test_encoder_controls_are_explicit(tmp_path: Path):
    media = MediaFile(tmp_path / "source.mkv", "h264", 10, 1)
    command = handbrake_command(
        "HandBrakeCLI",
        media,
        tmp_path / "output.part",
        [],
        [],
        None,
        18,
        "slow",
        bit_depth=12,
        tune="grain",
        crop="none",
        deinterlace="yadif",
        lossless=True,
    )
    assert "x265_12bit" in command
    assert "grain" in command
    assert "0:0:0:0" in command
    assert "--yadif" in command
    assert "lossless=1" in command


def test_recommended_format_autodetects_resolution():
    cases = [(480, "480p", 22), (720, "720p", 21), (1080, "1080p", 20), (2160, "4k", 18)]
    for height, resolution, quality in cases:
        media = MediaFile(Path("movie.mkv"), "h264", 10, 1, width=height * 16 // 9, height=height)
        settings = resolve_format_settings(media, "recommended", 30, "fast", 8, None)
        assert content_resolution(media) == resolution
        assert settings.quality == quality
        assert settings.bit_depth == 10
        assert settings.encoder_profile == "main10"


def test_recommended_command_builds_audio_variants_and_text_subtitles(tmp_path: Path):
    media = MediaFile(
        tmp_path / "movie.mkv",
        "h264",
        10,
        1,
        audio=[Track(1, 1, "audio", "eng", channels=6)],
        subtitles=[
            Track(2, 1, "subtitle", "eng", codec="subrip"),
            Track(3, 2, "subtitle", "eng", codec="hdmv_pgs_subtitle"),
        ],
        width=1920,
        height=1080,
    )
    settings = resolve_format_settings(media, "recommended", 18, "slow", 10, None)
    command = handbrake_command(
        "HandBrakeCLI",
        media,
        tmp_path / "out.part",
        ["eng"],
        ["eng"],
        None,
        settings.quality,
        settings.preset,
        bit_depth=settings.bit_depth,
        profile=settings.encoder_profile,
        library_audio=True,
        text_subtitles_only=True,
    )
    assert command[command.index("--quality") + 1] == "20.0"
    assert command[command.index("--audio") + 1] == "1,1"
    assert command[command.index("--ab") + 1] == "160,640"
    assert command[command.index("--mixdown") + 1] == "stereo,5point1"
    assert command[command.index("--subtitle") + 1] == "1"
    assert expected_audio_track_count(media, [1], True) == 2


def test_4k_hdr_preset_puts_eac3_first_and_preserves_metadata(tmp_path: Path):
    media = MediaFile(
        tmp_path / "movie.mkv",
        "h264",
        10,
        1,
        audio=[Track(1, 1, "audio", "eng", channels=8)],
        width=3840,
        height=2160,
        hdr=True,
    )
    settings = resolve_format_settings(media, "recommended", 20, "fast", 8, None)
    command = handbrake_command(
        "HandBrakeCLI",
        media,
        tmp_path / "out.part",
        ["eng"],
        [],
        None,
        settings.quality,
        settings.preset,
        bit_depth=settings.bit_depth,
        profile=settings.encoder_profile,
        library_audio=True,
    )
    assert command[command.index("--quality") + 1] == "18.0"
    assert command[command.index("--ab") + 1] == "768,160"
    assert command[command.index("--mixdown") + 1] == "5point1,stereo"
    assert command[command.index("--hdr-dynamic-metadata") + 1] == "all"
