from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
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
    "de": "deu",
    "deu": "deu",
    "ger": "deu",
    "german": "deu",
    "es": "spa",
    "spa": "spa",
    "spanish": "spa",
    "it": "ita",
    "ita": "ita",
    "italian": "ita",
    "ja": "jpn",
    "jpn": "jpn",
    "japanese": "jpn",
    "pt": "por",
    "por": "por",
    "portuguese": "por",
    "nl": "nld",
    "nld": "nld",
    "dut": "nld",
    "dutch": "nld",
    "zh": "zho",
    "zho": "zho",
    "chi": "zho",
    "chinese": "zho",
}


class BrakeSmithError(RuntimeError):
    pass


class ScanInterrupted(BrakeSmithError):
    def __init__(self, items: list[MediaFile], errors: list[str]):
        super().__init__(f"Scan cancelled after {len(items)} completed probes")
        self.items = items
        self.errors = errors


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
    codec: str = "unknown"
    channels: int = 0
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    visual_impaired: bool = False
    commentary: bool = False


@dataclass
class MediaFile:
    path: Path
    codec: str
    duration: float
    size: int
    audio: list[Track] = field(default_factory=list)
    subtitles: list[Track] = field(default_factory=list)
    original_language: str | None = None
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    color_space: str = ""
    field_order: str = ""
    frame_rate: str = ""
    hdr: bool = False
    dolby_vision: bool = False
    attachments: int = 0
    chapters: int = 0
    sidecars: list[Path] = field(default_factory=list)

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
    sources = {str(source.resolve()).casefold(): source for source, _ in destinations}
    for source, destination in destinations:
        key = str(destination.resolve()).casefold()
        previous = seen.get(key)
        if previous and previous != source:
            raise BrakeSmithError(
                f"Output collision: {previous} and {source} both map to {destination}"
            )
        if source.resolve() == destination.resolve():
            raise BrakeSmithError(f"Output would replace source: {source}")
        conflicting_source = sources.get(key)
        if conflicting_source and conflicting_source != source:
            raise BrakeSmithError(
                f"Output for {source} would overwrite another source: {conflicting_source}"
            )
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


def validate_output(
    path: Path,
    ffprobe: str,
    source_duration: float,
    expected_audio: int,
    expected_subtitles: int,
    expected_chapters: int | None = None,
) -> MediaFile:
    if not path.is_file() or path.stat().st_size < 1024:
        raise BrakeSmithError(f"Output is missing or too small: {path}")
    media = probe(path, ffprobe)
    if media.codec not in {"hevc", "h265"}:
        raise BrakeSmithError(f"Output video is {media.codec}, expected HEVC: {path}")
    tolerance = max(5.0, source_duration * 0.01)
    if source_duration and abs(media.duration - source_duration) > tolerance:
        raise BrakeSmithError(
            f"Output duration differs by {abs(media.duration - source_duration):.2f}s: {path}"
        )
    if len(media.audio) != expected_audio:
        raise BrakeSmithError(
            f"Output has {len(media.audio)} audio tracks, expected {expected_audio}: {path}"
        )
    if len(media.subtitles) != expected_subtitles:
        raise BrakeSmithError(
            f"Output has {len(media.subtitles)} subtitle tracks, expected {expected_subtitles}: {path}"
        )
    if media.attachments:
        raise BrakeSmithError(
            f"Output still has {media.attachments} image/data/attachment stream(s): {path}"
        )
    if expected_chapters is not None and media.chapters != expected_chapters:
        raise BrakeSmithError(
            f"Output has {media.chapters} chapters, expected {expected_chapters}: {path}"
        )
    return media


def quarantine_file(path: Path, label: str = "invalid") -> Path:
    candidate = path.with_name(f"{path.name}.{label}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{label}.{counter}")
        counter += 1
    try:
        path.replace(candidate)
    except OSError as error:
        raise BrakeSmithError(f"Cannot quarantine {path}: {error}") from error
    return candidate


def atomic_write_json(path: Path, payload: object, force: bool = False) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise BrakeSmithError(f"Report exists: {path}; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as error:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise BrakeSmithError(f"Cannot write report {path}: {error}") from error


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
    root: Path,
    max_depth: int | None = None,
    extra_extensions: Iterable[str] = (),
    errors: list[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise BrakeSmithError(f"Not a directory: {root}")
    extensions = VIDEO_EXTENSIONS | {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in extra_extensions
    }
    found = []

    def record_error(error: OSError) -> None:
        if errors is not None:
            errors.append(str(error))

    for directories, (current, dirs, files) in enumerate(
        os.walk(root, onerror=record_error), start=1
    ):
        relative = Path(current).relative_to(root)
        depth = len(relative.parts)
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []
        for filename in files:
            path = Path(current) / filename
            if path.suffix.lower() in extensions and not filename.endswith(".brakesmith.mkv"):
                found.append(path)
        if on_progress:
            on_progress(directories, len(found))
    return sorted(found, key=lambda path: str(path).lower())


def probe(path: Path, ffprobe: str = "ffprobe", timeout: float = 60) -> MediaFile:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=timeout
        )
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise BrakeSmithError(f"Cannot inspect {path}: {detail}") from error
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as error:
        raise BrakeSmithError(f"Cannot inspect {path}: {error}") from error
    video = next(
        (
            stream
            for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        {},
    )
    tracks: dict[str, list[Track]] = {"audio": [], "subtitle": []}
    counters = {"audio": 0, "subtitle": 0}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind not in tracks:
            continue
        counters[kind] += 1
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        language = normalize_languages([tags.get("language", "und")])[0]
        tracks[kind].append(
            Track(
                index=int(stream.get("index", -1)),
                type_index=counters[kind],
                kind=kind,
                language=language,
                title=tags.get("title", ""),
                codec=stream.get("codec_name", "unknown"),
                channels=int(stream.get("channels", 0) or 0),
                default=bool(disposition.get("default")),
                forced=bool(disposition.get("forced")),
                hearing_impaired=bool(disposition.get("hearing_impaired")),
                visual_impaired=bool(disposition.get("visual_impaired")),
                commentary=bool(disposition.get("comment")),
            )
        )
    format_data = data.get("format", {})
    tags = {str(k).lower(): v for k, v in format_data.get("tags", {}).items()}
    original = tags.get("original_language") or tags.get("language")
    normalized_original = normalize_languages([original])[0] if original else None
    transfer = video.get("color_transfer", "")
    side_data = video.get("side_data_list", [])
    dolby_vision = any(
        "dovi" in str(entry.get("side_data_type", "")).lower()
        or "dolby vision" in str(entry.get("side_data_type", "")).lower()
        for entry in side_data
    )
    sidecar_extensions = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
    sidecars = sorted(
        candidate
        for candidate in path.parent.glob(f"{path.stem}.*")
        if candidate != path and candidate.suffix.lower() in sidecar_extensions
    )
    return MediaFile(
        path=path,
        codec=video.get("codec_name", "unknown"),
        duration=float(format_data.get("duration", 0) or 0),
        size=path.stat().st_size,
        audio=tracks["audio"],
        subtitles=tracks["subtitle"],
        original_language=normalized_original,
        width=int(video.get("width", 0) or 0),
        height=int(video.get("height", 0) or 0),
        pixel_format=video.get("pix_fmt", ""),
        color_transfer=transfer,
        color_primaries=video.get("color_primaries", ""),
        color_space=video.get("color_space", ""),
        field_order=video.get("field_order", ""),
        frame_rate=video.get("avg_frame_rate", ""),
        hdr=transfer in {"smpte2084", "arib-std-b67"},
        dolby_vision=dolby_vision,
        attachments=sum(
            stream.get("codec_type") in {"attachment", "data"}
            or bool(stream.get("disposition", {}).get("attached_pic"))
            for stream in data.get("streams", [])
        ),
        chapters=sum(
            float(chapter.get("end_time", 0)) - float(chapter.get("start_time", 0)) >= 1.0
            for chapter in data.get("chapters", [])
        ),
        sidecars=sidecars,
    )


def default_cache_path() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "brakesmith" / "probe-cache.json"


class ProbeCache:
    def __init__(self, path: Path | None = None):
        self.path = (path or default_cache_path()).expanduser().resolve()
        self.entries: dict[str, dict[str, object]] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") == 3 and isinstance(payload.get("entries"), dict):
                self.entries = payload["entries"]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    def get(self, path: Path) -> MediaFile | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        entry = self.entries.get(str(path))
        if (
            not entry
            or entry.get("size") != stat.st_size
            or entry.get("modified_ns") != stat.st_mtime_ns
        ):
            return None
        try:
            return MediaFile(
                path=path,
                codec=str(entry["codec"]),
                duration=float(entry["duration"]),
                size=int(entry["size"]),
                audio=[Track(**track) for track in entry.get("audio", [])],
                subtitles=[Track(**track) for track in entry.get("subtitles", [])],
                original_language=entry.get("original_language"),
                width=int(entry.get("width", 0)),
                height=int(entry.get("height", 0)),
                pixel_format=str(entry.get("pixel_format", "")),
                color_transfer=str(entry.get("color_transfer", "")),
                color_primaries=str(entry.get("color_primaries", "")),
                color_space=str(entry.get("color_space", "")),
                field_order=str(entry.get("field_order", "")),
                frame_rate=str(entry.get("frame_rate", "")),
                hdr=bool(entry.get("hdr", False)),
                dolby_vision=bool(entry.get("dolby_vision", False)),
                attachments=int(entry.get("attachments", 0)),
                chapters=int(entry.get("chapters", 0)),
                sidecars=[Path(value) for value in entry.get("sidecars", [])],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, media: MediaFile) -> None:
        stat = media.path.stat()
        self.entries[str(media.path)] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "codec": media.codec,
            "duration": media.duration,
            "original_language": media.original_language,
            "audio": [vars(track) for track in media.audio],
            "subtitles": [vars(track) for track in media.subtitles],
            "width": media.width,
            "height": media.height,
            "pixel_format": media.pixel_format,
            "color_transfer": media.color_transfer,
            "color_primaries": media.color_primaries,
            "color_space": media.color_space,
            "field_order": media.field_order,
            "frame_rate": media.frame_rate,
            "hdr": media.hdr,
            "dolby_vision": media.dolby_vision,
            "attachments": media.attachments,
            "chapters": media.chapters,
            "sidecars": [str(path) for path in media.sidecars],
        }

    def save(self) -> None:
        atomic_write_json(self.path, {"version": 3, "entries": self.entries}, force=True)


def select_tracks(
    tracks: list[Track],
    languages: list[str],
    original: str | None = None,
    keep_commentary: bool = True,
    forced_only: bool = False,
    exclude_titles: Iterable[str] = (),
    fallback_default: bool = False,
) -> list[int]:
    wanted = set(normalize_languages(languages))
    if original:
        wanted.add(normalize_languages([original])[0])
    excluded = [value.casefold() for value in exclude_titles if value]
    eligible = [
        track
        for track in tracks
        if (keep_commentary or not track.commentary)
        and (not forced_only or track.forced)
        and not any(value in track.title.casefold() for value in excluded)
    ]
    selected = [track.type_index for track in eligible if track.language in wanted]
    if selected or not fallback_default:
        return selected
    defaults = [track.type_index for track in eligible if track.default]
    if defaults:
        return defaults
    return [eligible[0].type_index] if eligible and eligible[0].kind == "audio" else []


def fidelity_warnings(media: MediaFile) -> list[str]:
    warnings = []
    if media.dolby_vision:
        warnings.append("Dolby Vision detected; current encode may not preserve dynamic metadata")
    elif media.hdr:
        warnings.append("HDR detected; validate color and mastering metadata after encoding")
    if media.attachments:
        warnings.append(
            f"{media.attachments} image/data/attachment stream(s) detected; output removes them"
        )
    if media.sidecars:
        warnings.append(
            f"{len(media.sidecars)} external subtitle sidecar(s) are not embedded automatically"
        )
    if media.field_order and media.field_order not in {"progressive", "unknown"}:
        warnings.append(f"Interlaced field order detected: {media.field_order}")
    if media.width >= 3840 or media.height >= 2160:
        warnings.append(
            "4K source detected; encoding may require substantial time and temporary space"
        )
    return warnings


def output_path(
    source: Path, output_root: Path | None = None, scan_root: Path | None = None
) -> Path:
    if output_root is None:
        return source.with_name(f"{source.stem}.brakesmith.mkv")
    relative = source.relative_to(scan_root) if scan_root else Path(source.name)
    return (output_root / relative).with_suffix(".brakesmith.mkv")


def replacement_output_path(
    source: Path, output_root: Path | None = None, scan_root: Path | None = None
) -> Path:
    stem = source.stem
    replacements = (
        (r"(?i)(?<![a-z0-9])x264(?![a-z0-9])", "x265"),
        (r"(?i)(?<![a-z0-9])h\.264(?![a-z0-9])", "H.265"),
        (r"(?i)(?<![a-z0-9])h264(?![a-z0-9])", "H265"),
        (r"(?i)(?<![a-z0-9])avc(?![a-z0-9])", "HEVC"),
    )
    renamed_codec = False
    for pattern, replacement in replacements:
        stem, count = re.subn(pattern, replacement, stem)
        renamed_codec = renamed_codec or bool(count)
    if not renamed_codec and not re.search(
        r"(?i)(?<![a-z0-9])(?:x265|h\.?265|hevc)(?![a-z0-9])", stem
    ):
        stem = f"{stem}.x265"

    if output_root is None:
        destination = source.with_name(f"{stem}.mkv")
    else:
        relative = source.relative_to(scan_root) if scan_root else Path(source.name)
        destination = output_root / relative.parent / f"{stem}.mkv"
    if destination.resolve() == source.resolve():
        destination = destination.with_name(f"{destination.stem}.reencoded.mkv")
    return destination


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
    bit_depth: int = 10,
    tune: str | None = None,
    profile: str | None = None,
    level: str | None = None,
    crop: str = "auto",
    deinterlace: str = "auto",
    lossless: bool = False,
    audio_tracks: Iterable[int] | None = None,
    subtitle_tracks: Iterable[int] | None = None,
) -> list[str]:
    audio = (
        sorted(set(audio_tracks))
        if audio_tracks is not None
        else sorted(set(select_tracks(media.audio, audio_languages, original)) | set(extra_audio))
    )
    subtitles = (
        sorted(set(subtitle_tracks))
        if subtitle_tracks is not None
        else sorted(set(select_tracks(media.subtitles, subtitle_languages)) | set(extra_subtitles))
    )
    encoders = {8: "x265", 10: "x265_10bit", 12: "x265_12bit"}
    if bit_depth not in encoders:
        raise BrakeSmithError("Bit depth must be 8, 10, or 12")
    command = [
        executable,
        "-i",
        str(media.path),
        "-o",
        str(output),
        "--format",
        "av_mkv",
        "--encoder",
        encoders[bit_depth],
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
    if lossless:
        command += ["--encopts", "lossless=1"]
    if tune:
        command += ["--encoder-tune", tune]
    if profile:
        command += ["--encoder-profile", profile]
    if level:
        command += ["--encoder-level", level]
    if crop == "none":
        command += ["--crop", "0:0:0:0"]
    elif crop != "auto":
        raise BrakeSmithError("Crop policy must be auto or none")
    if deinterlace in {"decomb", "yadif"}:
        command += [f"--{deinterlace}"]
    elif deinterlace not in {"auto", "off"}:
        raise BrakeSmithError("Deinterlace policy must be auto, off, decomb, or yadif")
    command += (
        ["--audio", ",".join(map(str, audio)), "--aencoder", "copy"]
        if audio
        else ["--audio", "none"]
    )
    command += (
        ["--subtitle", ",".join(map(str, subtitles))] if subtitles else ["--subtitle", "none"]
    )
    return command
