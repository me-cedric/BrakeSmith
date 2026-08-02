from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".f4v",
    ".m2ts",
    ".m2v",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}
LANG_ALIASES = {
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    "fr": "fra",
    "fre": "fra",
    "fra": "fra",
    "french": "fra",
    "français": "fra",
    "und": "und",
    "unknown": "und",
}


class BrakeSmithError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    size: int
    modified_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class Track:
    index: int
    type_index: int
    kind: str
    language: str
    title: str = ""


@dataclass
class MediaFile:
    path: Path
    codec: str
    duration: float
    size: int
    audio: list[Track] = field(default_factory=list)
    subtitles: list[Track] = field(default_factory=list)
    original_language: str | None = None

    @property
    def should_convert(self) -> bool:
        return self.codec not in {"hevc", "h265"}


def snapshot_source(path: Path) -> SourceSnapshot:
    try:
        stat = path.stat()
    except OSError as error:
        raise BrakeSmithError(f"Cannot stat source {path}: {error}") from error
    if not path.is_file():
        raise BrakeSmithError(f"Source is no longer a regular file: {path}")
    return SourceSnapshot(path, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)


def ensure_source_unchanged(expected: SourceSnapshot) -> None:
    actual = snapshot_source(expected.path)
    if actual != expected:
        raise BrakeSmithError(f"Source changed since review: {expected.path}")


def validate_destinations(destinations: list[tuple[Path, Path]]) -> None:
    seen: dict[str, Path] = {}
    for source, destination in destinations:
        key = str(destination.resolve()).casefold()
        previous = seen.get(key)
        if previous and previous != source:
            raise BrakeSmithError(
                f"Output collision: {previous} and {source} both map to {destination}"
            )
        if source.resolve() == destination.resolve():
            raise BrakeSmithError(f"Output would replace source: {source}")
        seen[key] = source


def preflight_destination(destination: Path, expected_bytes: int) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(destination.parent, os.W_OK):
            raise BrakeSmithError(f"Output directory is not writable: {destination.parent}")
        free = shutil.disk_usage(destination.parent).free
    except OSError as error:
        raise BrakeSmithError(
            f"Cannot prepare output directory {destination.parent}: {error}"
        ) from error
    required = max(expected_bytes, 512 * 1024 * 1024)
    if free < required:
        raise BrakeSmithError(
            f"Insufficient free space for {destination}: need at least {required} bytes, have {free}"
        )


def normalize_languages(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        normalized = LANG_ALIASES.get(value.strip().lower(), value.strip().lower())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def find_executable(name: str, explicit: Path | None = None) -> str | None:
    if explicit:
        return str(explicit) if explicit.is_file() else None
    found = shutil.which(name)
    if found:
        return found
    if name == "HandBrakeCLI":
        candidates = [
            Path("/Applications/HandBrakeCLI"),
            Path("/usr/local/bin/HandBrakeCLI"),
            Path("/opt/homebrew/bin/HandBrakeCLI"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "HandBrake"
            / "HandBrakeCLI.exe",
        ]
        return next((str(path) for path in candidates if path.is_file()), None)
    return None


def discover(
    root: Path, max_depth: int | None = None, extra_extensions: Iterable[str] = ()
) -> list[Path]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise BrakeSmithError(f"Not a directory: {root}")
    extensions = VIDEO_EXTENSIONS | {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in extra_extensions
    }
    found = []
    for current, dirs, files in os.walk(root):
        relative = Path(current).relative_to(root)
        depth = len(relative.parts)
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []
        for filename in files:
            path = Path(current) / filename
            if path.suffix.lower() in extensions and not filename.endswith(".brakesmith.mkv"):
                found.append(path)
    return sorted(found, key=lambda path: str(path).lower())


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaFile:
    command = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise BrakeSmithError(f"Cannot inspect {path}: {error}") from error
    video = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"), {}
    )
    tracks: dict[str, list[Track]] = {"audio": [], "subtitle": []}
    counters = {"audio": 0, "subtitle": 0}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind not in tracks:
            continue
        counters[kind] += 1
        tags = stream.get("tags", {})
        language = normalize_languages([tags.get("language", "und")])[0]
        tracks[kind].append(
            Track(
                index=int(stream.get("index", -1)),
                type_index=counters[kind],
                kind=kind,
                language=language,
                title=tags.get("title", ""),
            )
        )
    format_data = data.get("format", {})
    tags = {str(k).lower(): v for k, v in format_data.get("tags", {}).items()}
    original = tags.get("original_language") or tags.get("language")
    normalized_original = normalize_languages([original])[0] if original else None
    return MediaFile(
        path=path,
        codec=video.get("codec_name", "unknown"),
        duration=float(format_data.get("duration", 0) or 0),
        size=path.stat().st_size,
        audio=tracks["audio"],
        subtitles=tracks["subtitle"],
        original_language=normalized_original,
    )


def select_tracks(
    tracks: list[Track], languages: list[str], original: str | None = None
) -> list[int]:
    wanted = set(normalize_languages(languages))
    if original:
        wanted.add(normalize_languages([original])[0])
    return [track.type_index for track in tracks if track.language in wanted]


def output_path(
    source: Path, output_root: Path | None = None, scan_root: Path | None = None
) -> Path:
    if output_root is None:
        return source.with_name(f"{source.stem}.brakesmith.mkv")
    relative = source.relative_to(scan_root) if scan_root else Path(source.name)
    return (output_root / relative).with_suffix(".brakesmith.mkv")


def handbrake_command(
    executable: str,
    media: MediaFile,
    output: Path,
    audio_languages: list[str],
    subtitle_languages: list[str],
    original: str | None,
    quality: float,
    preset: str,
    extra_audio: Iterable[int] = (),
    extra_subtitles: Iterable[int] = (),
) -> list[str]:
    audio = sorted(set(select_tracks(media.audio, audio_languages, original)) | set(extra_audio))
    subtitles = sorted(
        set(select_tracks(media.subtitles, subtitle_languages)) | set(extra_subtitles)
    )
    command = [
        executable,
        "-i",
        str(media.path),
        "-o",
        str(output),
        "--format",
        "av_mkv",
        "--encoder",
        "x265_10bit",
        "--quality",
        str(quality),
        "--encoder-preset",
        preset,
        "--markers",
        "--audio-copy-mask",
        "aac,ac3,eac3,truehd,dts,dtshd,mp3,flac",
        "--audio-fallback",
        "av_aac",
    ]
    command += (
        ["--audio", ",".join(map(str, audio)), "--aencoder", "copy"]
        if audio
        else ["--audio", "none"]
    )
    command += (
        ["--subtitle", ",".join(map(str, subtitles))] if subtitles else ["--subtitle", "none"]
    )
    return command
