from __future__ import annotations

import csv
import io
import json
import re
import signal
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

import pycountry
import questionary
import typer
from questionary import Choice
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .config import load_profile, prefer_profile
from .core import (
    BrakeSmithError,
    MediaFile,
    ProbeCache,
    ScanInterrupted,
    SourceSnapshot,
    atomic_write_json,
    discover,
    ensure_source_unchanged,
    fidelity_warnings,
    find_executable,
    handbrake_command,
    normalize_languages,
    output_path,
    preflight_destination,
    probe,
    quarantine_file,
    replacement_output_path,
    select_tracks,
    snapshot_source,
    validate_destinations,
    validate_output,
)
from .plans import load_plan, load_state, write_plan, write_state

app = typer.Typer(
    help="Forge a safe, reviewed batch of H.265 files with HandBrakeCLI.", no_args_is_help=True
)
console = Console()
error_console = Console(stderr=True)


def version_callback(value: bool) -> None:
    if value:
        console.print(f"BrakeSmith {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=version_callback, is_eager=True
    ),
) -> None:
    """Safety-first batch H.265 transcoding."""


def inspect(
    root: Path,
    depth: int,
    ffprobe_path: Optional[Path],
    extensions: str = "",
    workers: int = 2,
    probe_timeout: float = 60,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
) -> list[MediaFile]:
    ffprobe = find_executable("ffprobe", ffprobe_path)
    if not ffprobe:
        raise BrakeSmithError("ffprobe not found. Install FFmpeg or pass --ffprobe.")
    limit = None if depth < 0 else depth
    traversal_errors: list[str] = []
    discovery = Progress(
        SpinnerColumn(finished_text="[green]✓[/]"),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=error_console,
    )
    with discovery:
        discovery_task = discovery.add_task("Discovering videos", total=None)

        def update_discovery(directories: int, files: int) -> None:
            discovery.update(
                discovery_task,
                description=f"Discovering videos · {directories} folders · {files} found",
            )

        paths = discover(
            root,
            limit,
            extensions.split(",") if extensions else (),
            errors=traversal_errors,
            on_progress=update_discovery,
        )
        discovery.update(
            discovery_task,
            description=f"Discovered {len(paths)} videos",
            total=1,
            completed=1,
        )
    media: list[MediaFile] = []
    errors = list(traversal_errors)
    cache = ProbeCache(cache_path) if use_cache else None
    pending: list[Path] = []
    cache_hits = 0
    for path in paths:
        cached = cache.get(path) if cache else None
        if cached:
            media.append(cached)
            cache_hits += 1
        else:
            pending.append(path)

    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=error_console,
    )
    executor = ThreadPoolExecutor(
        max_workers=max(1, workers), thread_name_prefix="brakesmith-probe"
    )
    futures = {executor.submit(probe, path, ffprobe, probe_timeout): path for path in pending}
    try:
        with progress:
            task = progress.add_task(
                f"Analyzing metadata ({cache_hits} cached)",
                total=len(paths),
                completed=cache_hits,
            )
            for future in as_completed(futures):
                path = futures[future]
                try:
                    item = future.result()
                    media.append(item)
                    if cache:
                        cache.put(item)
                except BrakeSmithError as error:
                    errors.append(str(error))
                progress.advance(task)
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        if cache:
            try:
                cache.save()
            except BrakeSmithError as error:
                errors.append(str(error))
        raise ScanInterrupted(sorted(media, key=lambda item: str(item.path).lower()), errors)
    else:
        executor.shutdown(wait=True)
    if cache:
        try:
            cache.save()
        except BrakeSmithError as error:
            errors.append(str(error))
    for error in errors:
        error_console.print(f"[yellow]Warning:[/] {error}")
    error_console.print(
        f"[dim]Inspected {len(media)}/{len(paths)} files; {cache_hits} cache hits; "
        f"{len(errors)} warning(s).[/]"
    )
    return sorted(media, key=lambda item: str(item.path).lower())


def render(media: list[MediaFile], root: Path, view: str = "detailed") -> None:
    if view not in {"compact", "detailed"}:
        raise BrakeSmithError("View must be compact or detailed")
    table = Table(title=f"BrakeSmith scan · {root.resolve()}", show_lines=False)
    table.add_column("Status")
    table.add_column("File", overflow="fold")
    table.add_column("Codec")
    table.add_column("Video")
    if view == "detailed":
        table.add_column("Audio")
        table.add_column("Subtitles")
        table.add_column("Size", justify="right")
    else:
        table.add_column("Reason")
    for item in media:

        def track_label(track: object) -> str:
            flags = "".join(
                [
                    "D" if track.default else "",
                    "F" if track.forced else "",
                    "C" if track.commentary else "",
                    "H" if track.hearing_impaired else "",
                ]
            )
            return (
                f"{track.type_index}:{track.language}/{track.codec}{f'[{flags}]' if flags else ''}"
            )

        audio = ", ".join(track_label(track) for track in item.audio) or "—"
        subs = ", ".join(track_label(track) for track in item.subtitles) or "—"
        status = "[cyan]convert[/]" if item.should_convert else "[green]HEVC[/]"
        video = f"{item.width}×{item.height}" if item.width and item.height else "unknown"
        if item.dolby_vision:
            video += " DV"
        elif item.hdr:
            video += " HDR"
        relative = str(item.path.relative_to(root.resolve()))
        if view == "detailed":
            table.add_row(
                status,
                relative,
                item.codec,
                video,
                audio,
                subs,
                f"{item.size / 1_073_741_824:.2f} GB",
            )
        else:
            reason = "video codec is not HEVC" if item.should_convert else "already HEVC"
            table.add_row(status, relative, item.codec, video, reason)
    console.print(table)
    groups = Counter(
        (
            item.codec,
            f"{item.width}x{item.height}" if item.width and item.height else "unknown",
            "DV" if item.dolby_vision else "HDR" if item.hdr else "SDR",
        )
        for item in media
    )
    if groups:
        console.print(
            "[dim]Groups: "
            + "; ".join(
                f"{codec}/{resolution}/{dynamic_range}: {count}"
                for (codec, resolution, dynamic_range), count in sorted(groups.items())
            )
            + "[/]"
        )
    console.print(
        f"[bold]{len(media)}[/] video(s), [bold cyan]{sum(m.should_convert for m in media)}[/] need conversion"
    )


def display(media: list[MediaFile], root: Path, view: str, pager: bool = False) -> None:
    if pager and console.is_terminal:
        with console.pager(styles=True):
            render(media, root, view)
    else:
        render(media, root, view)


def media_payload(item: MediaFile) -> dict[str, object]:
    return {
        "path": str(item.path),
        "codec": item.codec,
        "should_convert": item.should_convert,
        "duration": item.duration,
        "size": item.size,
        "original_language": item.original_language,
        "audio": [vars(track) for track in item.audio],
        "subtitles": [vars(track) for track in item.subtitles],
        "video": {
            "width": item.width,
            "height": item.height,
            "pixel_format": item.pixel_format,
            "frame_rate": item.frame_rate,
            "field_order": item.field_order,
            "color_transfer": item.color_transfer,
            "color_primaries": item.color_primaries,
            "color_space": item.color_space,
            "hdr": item.hdr,
            "dolby_vision": item.dolby_vision,
        },
        "attachments": item.attachments,
        "chapters": item.chapters,
        "sidecars": [str(path) for path in item.sidecars],
        "warnings": fidelity_warnings(item),
    }


def write_candidates_report(items: list[MediaFile], output: Path, force: bool = False) -> None:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise BrakeSmithError(f"Report exists: {output}; pass --force to replace it")
    suffix = output.suffix.lower()
    if suffix == ".json":
        content = json.dumps([media_payload(item) for item in items], indent=2) + "\n"
    elif suffix == ".csv":
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["path", "codec", "duration_seconds", "size_bytes", "audio", "subtitles"])
        for item in items:
            writer.writerow(
                [
                    item.path,
                    item.codec,
                    item.duration,
                    item.size,
                    ",".join(track.language for track in item.audio),
                    ",".join(track.language for track in item.subtitles),
                ]
            )
        content = stream.getvalue()
    elif suffix == ".txt":
        content = "".join(f"{item.path}\n" for item in items)
    else:
        raise BrakeSmithError("Report extension must be .json, .csv, or .txt")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


@app.command()
def scan(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(
        -1, help="Subdirectory depth; -1 means recursive, 0 means this directory only."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
    view: str = typer.Option("detailed", help="compact or detailed table."),
    pager: bool = typer.Option(False, help="Page interactive table output."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    workers: int = typer.Option(2, min=1, max=32, help="Concurrent metadata probes."),
    probe_timeout: float = typer.Option(60, min=1, help="Seconds allowed per metadata probe."),
    cache: Optional[Path] = typer.Option(None, "--cache-file", help="Local probe-cache path."),
    use_cache: bool = typer.Option(True, "--cache/--no-cache", help="Use local probe cache."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """List every supported video and whether it should be converted."""
    try:
        items = inspect(
            directory, depth, ffprobe, extensions, workers, probe_timeout, cache, use_cache
        )
    except ScanInterrupted as error:
        error_console.print(f"[yellow]{error}[/]")
        raise typer.Exit(130)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    if json_output:
        payload = [media_payload(item) for item in items]
        typer.echo(json.dumps(payload, indent=2))
    else:
        display(items, directory, view, pager)


@app.command()
def candidates(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    output: Optional[Path] = typer.Option(None, help="Write complete .json, .csv, or .txt list."),
    force: bool = typer.Option(False, help="Replace an existing report, never media."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    workers: int = typer.Option(2, min=1, max=32, help="Concurrent metadata probes."),
    probe_timeout: float = typer.Option(60, min=1, help="Seconds allowed per metadata probe."),
    cache: Optional[Path] = typer.Option(None, "--cache-file", help="Local probe-cache path."),
    use_cache: bool = typer.Option(True, "--cache/--no-cache", help="Use local probe cache."),
    include: str = typer.Option("", help="Comma-separated relative-path globs to include."),
    exclude: str = typer.Option("", help="Comma-separated relative-path globs to exclude."),
    min_size: int = typer.Option(0, min=0, help="Minimum source bytes."),
    max_size: int = typer.Option(0, min=0, help="Maximum source bytes; 0 disables."),
    min_duration: float = typer.Option(0, min=0, help="Minimum duration in seconds."),
    max_duration: float = typer.Option(0, min=0, help="Maximum duration; 0 disables."),
    codecs: str = typer.Option("", help="Comma-separated source codecs to include."),
    view: str = typer.Option("compact", help="compact or detailed table."),
    pager: bool = typer.Option(False, help="Page interactive table output."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """List only files whose video codec is not already HEVC."""
    try:
        items = inspect(
            directory, depth, ffprobe, extensions, workers, probe_timeout, cache, use_cache
        )
        include_patterns = [value for value in include.split(",") if value]
        exclude_patterns = [value for value in exclude.split(",") if value]
        wanted_codecs = {value.strip().lower() for value in codecs.split(",") if value.strip()}
        items = [
            item
            for item in items
            if item.should_convert
            and (
                not include_patterns
                or any(
                    fnmatch(str(item.path.relative_to(directory)), pattern)
                    for pattern in include_patterns
                )
            )
            and not any(
                fnmatch(str(item.path.relative_to(directory)), pattern)
                for pattern in exclude_patterns
            )
            and item.size >= min_size
            and (not max_size or item.size <= max_size)
            and item.duration >= min_duration
            and (not max_duration or item.duration <= max_duration)
            and (not wanted_codecs or item.codec.lower() in wanted_codecs)
        ]
        if output:
            write_candidates_report(items, output, force)
            console.print(f"[green]Saved:[/] {len(items)} conversion candidate(s) to {output}")
        else:
            display(items, directory, view, pager)
    except ScanInterrupted as error:
        if output:
            partial = output.with_name(f"{output.stem}.partial{output.suffix}")
            try:
                write_candidates_report(
                    [item for item in error.items if item.should_convert], partial, force=True
                )
                error_console.print(f"[yellow]Partial report saved:[/] {partial.resolve()}")
            except BrakeSmithError as report_error:
                error_console.print(f"[red]Partial report failed:[/] {report_error}")
        error_console.print(f"[yellow]{error}[/]")
        raise typer.Exit(130)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)


@app.command()
def doctor(
    handbrake: Optional[Path] = typer.Option(None, help="Path to HandBrakeCLI."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """Check required tools without changing files."""
    checks = {
        "HandBrakeCLI": find_executable("HandBrakeCLI", handbrake),
        "ffprobe": find_executable("ffprobe", ffprobe),
    }
    for name, path in checks.items():
        console.print(f"[green]✓[/] {name}: {path}" if path else f"[red]✗[/] {name}: not found")
    if not all(checks.values()):
        console.print("Install HandBrakeCLI and FFmpeg, or pass explicit paths.")
        raise typer.Exit(1)


def reconcile_original(
    items: list[MediaFile], keep_original: bool, assume: Optional[str], yes: bool
) -> dict[Path, Optional[str]]:
    result: dict[Path, Optional[str]] = {}
    assumed = normalize_languages([assume])[0] if assume else None
    for item in items:
        if not keep_original:
            result[item.path] = None
        elif item.original_language:
            result[item.path] = item.original_language
        elif assumed:
            result[item.path] = assumed
        elif yes:
            raise BrakeSmithError(
                f"Original language unknown for {item.path}; use --original-language or omit --keep-original."
            )
        else:
            choices = sorted({track.language for track in item.audio})
            answer = Prompt.ask(
                f"Original language for [bold]{item.path.name}[/] ({', '.join(choices)})",
                choices=choices + ["none"],
                default="none",
            )
            result[item.path] = None if answer == "none" else answer
    return result


def language_name(code: str) -> str:
    if code == "und":
        return "Undefined"
    language = pycountry.languages.get(alpha_3=code) or pycountry.languages.get(alpha_2=code)
    return str(language.name) if language else code.upper()


def reconcile_quality_profile(
    quality: float, preset: str, non_interactive: bool
) -> tuple[float, str]:
    if non_interactive:
        return quality, preset
    profiles = {
        "highest": (16.0, "slow"),
        "high": (18.0, "slow"),
        "balanced": (20.0, "medium"),
        "compact": (22.0, "medium"),
        "custom": (quality, preset),
    }
    answer = questionary.select(
        "Choose quality profile",
        choices=[
            Choice("Highest practical quality — RF 16, slow", value="highest"),
            Choice("High quality — RF 18, slow", value="high"),
            Choice("Balanced — RF 20, medium", value="balanced"),
            Choice("Compact — RF 22, medium", value="compact"),
            Choice(f"Current CLI settings — RF {quality:g}, {preset}", value="custom"),
        ],
        default="highest",
        instruction="(↑/↓ move, enter confirm)",
    ).ask()
    if answer is None:
        raise BrakeSmithError("Quality selection cancelled")
    return profiles[answer]


def reconcile_max_files(configured: Optional[int], non_interactive: bool) -> Optional[int]:
    if configured is not None or non_interactive:
        return configured

    def validate(value: str) -> bool | str:
        return value.isdigit() and int(value) > 0 or "Enter a whole number greater than zero"

    answer = questionary.text(
        "Maximum files to propose for transcoding",
        default="1",
        validate=validate,
    ).ask()
    if answer is None:
        raise BrakeSmithError("Maximum file selection cancelled")
    return int(answer)


def limit_proposed_files(items: list[MediaFile], maximum: Optional[int]) -> list[MediaFile]:
    if maximum is None or len(items) <= maximum:
        return items
    console.print(f"Proposing first {maximum} of {len(items)} conversion candidate(s).")
    return items[:maximum]


def reconcile_languages(
    items: list[MediaFile],
    configured_audio: str,
    configured_subtitles: str,
    non_interactive: bool,
    choose_unknown_audio: bool,
    choose_unknown_subtitles: bool,
) -> tuple[list[str], list[str], Optional[bool], Optional[bool]]:
    audio_languages = normalize_languages(configured_audio.split(","))
    subtitle_languages = normalize_languages(configured_subtitles.split(","))
    audio_counts = Counter(track.language for item in items for track in item.audio)
    subtitle_counts = Counter(track.language for item in items for track in item.subtitles)
    counts = audio_counts + subtitle_counts
    if non_interactive or not counts:
        return audio_languages, subtitle_languages, None, None

    configured = list(dict.fromkeys([*audio_languages, *subtitle_languages]))
    ordered = [
        *[language for language in configured if language in counts],
        *sorted(counts.keys() - set(configured) - {"und"}),
    ]
    choose_unknown = (choose_unknown_audio and "und" in audio_counts) or (
        choose_unknown_subtitles and "und" in subtitle_counts
    )
    if choose_unknown:
        ordered.append("und")
    if not ordered:
        return audio_languages, subtitle_languages, None, None
    only_undefined = set(counts) == {"und"}
    answer = questionary.checkbox(
        "Choose languages to keep for audio and subtitles",
        choices=[
            Choice(
                f"{language_name(language)} — {audio_counts[language]} audio, "
                f"{subtitle_counts[language]} subtitle",
                value=language,
                checked=language in configured or (language == "und" and only_undefined),
            )
            for language in ordered
        ],
        instruction="(↑/↓ move, space toggle, enter confirm)",
    ).ask()
    if answer is None:
        raise BrakeSmithError("Language selection cancelled")
    selected = [language for language in answer if language != "und"]
    unknown = "und" in answer
    return (
        selected,
        selected,
        unknown if choose_unknown_audio and "und" in audio_counts else None,
        unknown if choose_unknown_subtitles and "und" in subtitle_counts else None,
    )


def reconcile_unknown_tracks(
    items: list[MediaFile], kind: str, policy: str, non_interactive: bool
) -> dict[Path, set[int]]:
    assigned_language: Optional[str] = None
    if policy.startswith("language:"):
        value = policy.partition(":")[2]
        if not value:
            raise BrakeSmithError(f"--unknown-{kind} language assignment needs a code")
        assigned_language = normalize_languages([value])[0]
    elif policy not in {"ask", "keep", "drop"}:
        raise BrakeSmithError(f"--unknown-{kind} must be ask, keep, drop, or language:CODE")
    result: dict[Path, set[int]] = {item.path: set() for item in items}
    unknown = [
        (item, track)
        for item in items
        for track in (item.audio if kind == "audio" else item.subtitles)
        if track.language == "und"
    ]
    if policy == "ask" and unknown:
        if non_interactive:
            raise BrakeSmithError(
                f"{len(unknown)} unlabelled {kind} track(s); use --unknown-{kind} keep or drop."
            )
        policy = (
            "keep"
            if Confirm.ask(
                f"Keep all {len(unknown)} unlabelled {kind} track(s) across this batch?",
                default=kind == "audio",
            )
            else "drop"
        )
    for item in items:
        tracks = item.audio if kind == "audio" else item.subtitles
        for track in (candidate for candidate in tracks if candidate.language == "und"):
            if policy == "keep" or assigned_language:
                result[item.path].add(track.type_index)
    return result


@app.command(name="plan")
def plan_batch(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    output: Path = typer.Option(..., help="Destination .json plan."),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    max_files: Optional[int] = typer.Option(
        None, min=1, help="Maximum conversion candidates; interactive runs ask when omitted."
    ),
    audio: str = typer.Option("eng,fra", help="Audio languages to keep."),
    subtitles: str = typer.Option("eng,fra", help="Subtitle languages to keep."),
    unknown_audio: str = typer.Option("ask", help="Unlabelled audio: ask, keep, or drop."),
    unknown_subtitles: str = typer.Option("ask", help="Unlabelled subtitles: ask, keep, or drop."),
    keep_commentary: bool = typer.Option(False, help="Keep commentary tracks."),
    forced_subtitles_only: bool = typer.Option(False, help="Keep only forced requested subtitles."),
    exclude_titles: str = typer.Option(
        "commentary,description", help="Track-title fragments to exclude."
    ),
    keep_original: bool = typer.Option(False, "--keep-original/--no-keep-original"),
    original_language: Optional[str] = typer.Option(None),
    quality: float = typer.Option(18.0, min=0, max=51),
    preset: str = typer.Option("slow"),
    bit_depth: int = typer.Option(10, min=8, max=12),
    tune: Optional[str] = typer.Option(None),
    encoder_profile: Optional[str] = typer.Option(None),
    encoder_level: Optional[str] = typer.Option(None),
    crop: str = typer.Option("auto", help="Crop policy: auto or none."),
    deinterlace: str = typer.Option("auto", help="auto, off, decomb, or yadif."),
    lossless: bool = typer.Option(False, help="x265 lossless mode; output may be huge."),
    output_directory: Optional[Path] = typer.Option(None),
    replace_source: bool = typer.Option(
        False,
        "--replace-source/--keep-source",
        help="Replace each source only when its validated HEVC output is smaller.",
    ),
    overrides: Optional[Path] = typer.Option(
        None, exists=True, dir_okay=False, help="Per-source audio_tracks/subtitle_tracks JSON."
    ),
    allow_no_audio: bool = typer.Option(False),
    extensions: str = typer.Option(""),
    workers: int = typer.Option(2, min=1, max=32),
    probe_timeout: float = typer.Option(60, min=1),
    cache: Optional[Path] = typer.Option(None, "--cache-file"),
    use_cache: bool = typer.Option(True, "--cache/--no-cache"),
    non_interactive: bool = typer.Option(
        False, help="Forbid all prompts; unresolved choices fail."
    ),
    profile: Optional[str] = typer.Option(None, help="Named TOML profile."),
    config: Optional[Path] = typer.Option(None, help="TOML config path."),
    force: bool = typer.Option(False, help="Replace only an existing plan."),
    handbrake: Optional[Path] = typer.Option(None),
    ffprobe: Optional[Path] = typer.Option(None),
) -> None:
    """Create a reviewed, immutable batch plan without encoding."""
    try:
        profile_settings = load_profile(profile, config)
        audio = str(prefer_profile(profile_settings, "audio", audio, "eng,fra"))
        subtitles = str(prefer_profile(profile_settings, "subtitles", subtitles, "eng,fra"))
        unknown_audio = str(prefer_profile(profile_settings, "unknown_audio", unknown_audio, "ask"))
        unknown_subtitles = str(
            prefer_profile(profile_settings, "unknown_subtitles", unknown_subtitles, "ask")
        )
        quality = float(prefer_profile(profile_settings, "quality", quality, 18.0))
        preset = str(prefer_profile(profile_settings, "preset", preset, "slow"))
        bit_depth = int(prefer_profile(profile_settings, "bit_depth", bit_depth, 10))
        crop = str(prefer_profile(profile_settings, "crop", crop, "auto"))
        deinterlace = str(prefer_profile(profile_settings, "deinterlace", deinterlace, "auto"))
        workers = int(prefer_profile(profile_settings, "workers", workers, 2))
        probe_timeout = float(prefer_profile(profile_settings, "probe_timeout", probe_timeout, 60))
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    executable = find_executable("HandBrakeCLI", handbrake)
    ffprobe_executable = find_executable("ffprobe", ffprobe)
    if not executable or not ffprobe_executable:
        console.print("[red]Error:[/] HandBrakeCLI or ffprobe not found. Run `brakesmith doctor`.")
        raise typer.Exit(2)
    try:
        quality, preset = reconcile_quality_profile(quality, preset, non_interactive)
        max_files = reconcile_max_files(max_files, non_interactive)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    try:
        items = limit_proposed_files(
            [
                item
                for item in inspect(
                    directory,
                    depth,
                    ffprobe,
                    extensions,
                    workers,
                    probe_timeout,
                    cache,
                    use_cache,
                )
                if item.should_convert
            ],
            max_files,
        )
        if not items:
            console.print("No conversion candidates.")
            return
        (
            audio_languages,
            subtitle_languages,
            unknown_audio_choice,
            unknown_subtitle_choice,
        ) = reconcile_languages(
            items,
            audio,
            subtitles,
            non_interactive,
            choose_unknown_audio=unknown_audio == "ask",
            choose_unknown_subtitles=unknown_subtitles == "ask",
        )
        if unknown_audio_choice is not None:
            unknown_audio = "keep" if unknown_audio_choice else "drop"
        if unknown_subtitle_choice is not None:
            unknown_subtitles = "keep" if unknown_subtitle_choice else "drop"
        originals = reconcile_original(items, keep_original, original_language, non_interactive)
        extra_audio = reconcile_unknown_tracks(items, "audio", unknown_audio, non_interactive)
        extra_subtitles = reconcile_unknown_tracks(
            items, "subtitles", unknown_subtitles, non_interactive
        )
        excluded_titles = [value.strip() for value in exclude_titles.split(",") if value.strip()]
        override_map: dict[str, object] = {}
        if overrides:
            try:
                loaded_overrides = json.loads(overrides.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise BrakeSmithError(f"Cannot read overrides {overrides}: {error}") from error
            if not isinstance(loaded_overrides, dict):
                raise BrakeSmithError("Overrides must be a JSON object keyed by source path")
            override_map = loaded_overrides
        plan_items: list[dict[str, object]] = []
        destinations: list[tuple[Path, Path]] = []
        for item in items:
            selected_audio = sorted(
                set(
                    select_tracks(
                        item.audio,
                        audio_languages,
                        originals[item.path],
                        keep_commentary=keep_commentary,
                        exclude_titles=excluded_titles,
                        fallback_default=True,
                    )
                )
                | extra_audio[item.path]
            )
            selected_subtitles = sorted(
                set(
                    select_tracks(
                        item.subtitles,
                        subtitle_languages,
                        keep_commentary=keep_commentary,
                        forced_only=forced_subtitles_only,
                        exclude_titles=excluded_titles,
                        fallback_default=True,
                    )
                )
                | extra_subtitles[item.path]
            )
            override = override_map.get(str(item.path)) or override_map.get(
                str(item.path.relative_to(directory))
            )
            if override:
                if not isinstance(override, dict):
                    raise BrakeSmithError(f"Invalid override for {item.path}")
                selected_audio = sorted(
                    int(value) for value in override.get("audio_tracks", selected_audio)
                )
                selected_subtitles = sorted(
                    int(value) for value in override.get("subtitle_tracks", selected_subtitles)
                )
                valid_audio = {track.type_index for track in item.audio}
                valid_subtitles = {track.type_index for track in item.subtitles}
                if (
                    not set(selected_audio) <= valid_audio
                    or not set(selected_subtitles) <= valid_subtitles
                ):
                    raise BrakeSmithError(f"Override references missing track for {item.path}")
            if item.audio and not selected_audio and not allow_no_audio:
                raise BrakeSmithError(f"No audio selected for {item.path}; adjust policy")
            destination = (
                replacement_output_path(item.path, output_directory, directory)
                if replace_source
                else output_path(item.path, output_directory, directory)
            )
            destinations.append((item.path, destination))
            partial = destination.with_name(destination.name + ".part")
            snapshot = snapshot_source(item.path)
            plan_items.append(
                {
                    "source": str(item.path),
                    "destination": str(destination),
                    "partial": str(partial),
                    "snapshot": {
                        "path": str(snapshot.path),
                        "size": snapshot.size,
                        "modified_ns": snapshot.modified_ns,
                        "device": snapshot.device,
                        "inode": snapshot.inode,
                    },
                    "duration": item.duration,
                    "chapters": item.chapters,
                    "audio_tracks": selected_audio,
                    "subtitle_tracks": selected_subtitles,
                    "replace_source": replace_source,
                    "warnings": fidelity_warnings(item),
                    "command": handbrake_command(
                        executable,
                        item,
                        partial,
                        audio_languages,
                        subtitle_languages,
                        originals[item.path],
                        quality,
                        preset,
                        extra_audio[item.path],
                        extra_subtitles[item.path],
                        bit_depth,
                        tune,
                        encoder_profile,
                        encoder_level,
                        crop,
                        deinterlace,
                        lossless,
                        selected_audio,
                        selected_subtitles,
                    ),
                }
            )
        validate_destinations(destinations)
        for source, destination in destinations:
            console.print(f"[dim]{source} → {destination} · collision-free[/]")
        for item in plan_items:
            for warning in item["warnings"]:
                console.print(f"[yellow]Warning:[/] {Path(str(item['source'])).name}: {warning}")
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "root": str(directory),
            "ffprobe": ffprobe_executable,
            "settings": {
                "quality": quality,
                "preset": preset,
                "bit_depth": bit_depth,
                "tune": tune,
                "profile": encoder_profile,
                "level": encoder_level,
                "crop": crop,
                "deinterlace": deinterlace,
                "lossless": lossless,
                "audio_languages": audio_languages,
                "subtitle_languages": subtitle_languages,
                "replace_source": replace_source,
            },
            "totals": {
                "files": len(plan_items),
                "source_bytes": sum(item.size for item in items),
                "minimum_free_bytes": sum(item.size for item in items),
                "duration_seconds": sum(item.duration for item in items),
            },
            "items": plan_items,
        }
        write_plan(output, payload, force)
        if replace_source:
            console.print(
                "[bold red]Replace mode:[/] execution deletes a source only after its output "
                "is validated and smaller."
            )
        console.print(
            f"[green]Plan:[/] {output.resolve()} · {len(plan_items)} file(s) · "
            f"{sum(item.duration for item in items) / 3600:.1f} hours"
        )
    except ScanInterrupted as error:
        error_console.print(f"[yellow]{error}[/]")
        raise typer.Exit(130)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)


app.command(name="dry-run", help="Alias for plan; writes a non-destructive reviewed plan.")(
    plan_batch
)


@app.command(name="execute")
def execute_plan(
    plan_file: Path = typer.Argument(..., exists=True, dir_okay=False, resolve_path=True),
    state_file: Optional[Path] = typer.Option(None, help="Atomic state journal path."),
    retry_failed: bool = typer.Option(False, help="Process only previously failed items."),
    stop_after_current: bool = typer.Option(False, help="Stop cleanly after one attempted file."),
    max_failures: int = typer.Option(
        1, min=0, help="Stop after this many failures; 0 is unlimited."
    ),
) -> None:
    """Execute an immutable plan with durable resume state."""
    try:
        plan = load_plan(plan_file)
        plan_digest = str(plan["digest"])
        journal_path = state_file or plan_file.with_name(f"{plan_file.stem}.state.json")
        state = load_state(journal_path, plan_digest)
        statuses = state["items"]
        assert isinstance(statuses, dict)
        items = plan["items"]
        assert isinstance(items, list)
        ffprobe_executable = str(plan["ffprobe"])
        if not Path(ffprobe_executable).is_file():
            raise BrakeSmithError(f"Planned ffprobe is unavailable: {ffprobe_executable}")
        durations = [float(item["duration"]) for item in items]
        weights = [duration or 1.0 for duration in durations]
        total_duration = sum(weights)
        completed_weight = 0.0
        failures = 0
        attempted = 0
        progress_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def request_cancel(signum: int, frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, request_cancel)
        try:
            with Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                total_task = progress.add_task("Planned duration", total=total_duration)
                for file_number, (item, duration, weight) in enumerate(
                    zip(items, durations, weights), start=1
                ):
                    source = Path(str(item["source"]))
                    destination = Path(str(item["destination"]))
                    partial = Path(str(item["partial"]))
                    previous = statuses.get(str(source), {})
                    previous_status = previous.get("status") if isinstance(previous, dict) else None
                    if retry_failed and previous_status != "failed":
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        continue
                    if not retry_failed and previous_status == "completed":
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        continue
                    attempted += 1
                    file_task = progress.add_task(
                        f"[{file_number}/{len(items)}] {source.name}", total=100
                    )
                    diagnostics: list[str] = []
                    process: Optional[subprocess.Popen[str]] = None
                    try:
                        raw_snapshot = item["snapshot"]
                        assert isinstance(raw_snapshot, dict)
                        snapshot = SourceSnapshot(
                            path=Path(str(raw_snapshot["path"])),
                            size=int(raw_snapshot["size"]),
                            modified_ns=int(raw_snapshot["modified_ns"]),
                            device=int(raw_snapshot["device"]),
                            inode=int(raw_snapshot["inode"]),
                        )
                        replace_source = bool(item.get("replace_source", False))
                        expected_audio = len(item["audio_tracks"])
                        expected_subtitles = len(item["subtitle_tracks"])
                        if not source.exists() and replace_source and destination.exists():
                            validate_output(
                                destination,
                                ffprobe_executable,
                                duration,
                                expected_audio,
                                expected_subtitles,
                                int(item.get("chapters", 0)),
                            )
                            statuses[str(source)] = {
                                "status": "completed",
                                "output": str(destination),
                                "recovered": True,
                                "source_deleted": True,
                            }
                            progress.update(file_task, completed=100)
                            continue
                        ensure_source_unchanged(snapshot)
                        if destination.exists():
                            validate_output(
                                destination,
                                ffprobe_executable,
                                duration,
                                expected_audio,
                                expected_subtitles,
                                int(item.get("chapters", 0)),
                            )
                            discarded_size = (
                                discard_not_smaller(destination, snapshot.size)
                                if replace_source
                                else None
                            )
                            if replace_source and discarded_size is None:
                                delete_replaced_source(source, destination)
                            statuses[str(source)] = {
                                "status": "completed",
                                "output": None if discarded_size is not None else str(destination),
                                "recovered": True,
                                "source_deleted": replace_source and discarded_size is None,
                                "result": (
                                    "kept-source-not-smaller"
                                    if discarded_size is not None
                                    else "published"
                                ),
                                "source_bytes": snapshot.size,
                                "output_bytes": discarded_size,
                            }
                            if discarded_size is not None:
                                console.print(
                                    f"[yellow]Kept source:[/] output was not smaller "
                                    f"({discarded_size} >= {snapshot.size} bytes): {source}"
                                )
                        else:
                            if partial.exists():
                                ensure_source_unchanged(snapshot)
                                validate_output(
                                    partial,
                                    ffprobe_executable,
                                    duration,
                                    expected_audio,
                                    expected_subtitles,
                                    int(item.get("chapters", 0)),
                                )
                                discarded_size = (
                                    discard_not_smaller(partial, snapshot.size)
                                    if replace_source
                                    else None
                                )
                                if discarded_size is None:
                                    partial.replace(destination)
                                    if replace_source:
                                        delete_replaced_source(source, destination)
                                statuses[str(source)] = {
                                    "status": "completed",
                                    "output": (
                                        None if discarded_size is not None else str(destination)
                                    ),
                                    "recovered_partial": True,
                                    "source_deleted": replace_source and discarded_size is None,
                                    "result": (
                                        "kept-source-not-smaller"
                                        if discarded_size is not None
                                        else "published"
                                    ),
                                    "source_bytes": snapshot.size,
                                    "output_bytes": discarded_size,
                                }
                                if discarded_size is not None:
                                    console.print(
                                        f"[yellow]Kept source:[/] output was not smaller "
                                        f"({discarded_size} >= {snapshot.size} bytes): {source}"
                                    )
                            else:
                                preflight_destination(destination, snapshot.size)
                                command = item["command"]
                                if not isinstance(command, list) or not all(
                                    isinstance(value, str) for value in command
                                ):
                                    raise BrakeSmithError(f"Invalid planned command for {source}")
                                process = subprocess.Popen(
                                    command,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True,
                                    errors="replace",
                                )
                                assert process.stdout is not None
                                for line in process.stdout:
                                    diagnostics.append(line)
                                    match = progress_pattern.search(line)
                                    if match:
                                        percent = min(float(match.group(1)), 100)
                                        progress.update(file_task, completed=percent)
                                        progress.update(
                                            total_task,
                                            completed=completed_weight + weight * percent / 100,
                                        )
                                if process.wait() != 0:
                                    raise BrakeSmithError(f"HandBrake failed for {source.name}")
                                ensure_source_unchanged(snapshot)
                                validate_output(
                                    partial,
                                    ffprobe_executable,
                                    duration,
                                    expected_audio,
                                    expected_subtitles,
                                    int(item.get("chapters", 0)),
                                )
                                discarded_size = (
                                    discard_not_smaller(partial, snapshot.size)
                                    if replace_source
                                    else None
                                )
                                if discarded_size is None:
                                    partial.replace(destination)
                                    if replace_source:
                                        delete_replaced_source(source, destination)
                                statuses[str(source)] = {
                                    "status": "completed",
                                    "output": (
                                        None if discarded_size is not None else str(destination)
                                    ),
                                    "source_deleted": replace_source and discarded_size is None,
                                    "result": (
                                        "kept-source-not-smaller"
                                        if discarded_size is not None
                                        else "published"
                                    ),
                                    "source_bytes": snapshot.size,
                                    "output_bytes": discarded_size,
                                }
                                if discarded_size is not None:
                                    console.print(
                                        f"[yellow]Kept source:[/] output was not smaller "
                                        f"({discarded_size} >= {snapshot.size} bytes): {source}"
                                    )
                        progress.update(file_task, completed=100)
                    except KeyboardInterrupt:
                        terminate_process(process)
                        cleanup_error = cleanup_partial(partial)
                        statuses[str(source)] = {
                            "status": "cancelled",
                            "error": cleanup_error,
                        }
                        write_state(journal_path, state)
                        console.print(
                            "\n[yellow]Cancelled.[/] State saved; completed replacements remain."
                        )
                        raise typer.Exit(130)
                    except Exception as error:  # noqa: BLE001 - durable state needs every failure
                        terminate_process(process)
                        quarantined: Optional[Path] = None
                        if partial.exists():
                            try:
                                quarantined = quarantine_file(partial, "invalid")
                            except BrakeSmithError:
                                pass
                        log_path: Optional[Path] = None
                        if diagnostics:
                            try:
                                log_path = write_failure_log(destination, diagnostics)
                            except OSError:
                                pass
                        failures += 1
                        statuses[str(source)] = {
                            "status": "failed",
                            "error": str(error),
                            "quarantined": str(quarantined) if quarantined else None,
                            "log": str(log_path) if log_path else None,
                        }
                        console.print(f"[red]Failed:[/] {source}: {error}")
                    finally:
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        progress.remove_task(file_task)
                        write_state(journal_path, state)
                    if stop_after_current or (max_failures and failures >= max_failures):
                        break
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        console.print(
            f"[green]State:[/] {journal_path.resolve()} · {attempted} attempted · {failures} failed"
        )
        if failures:
            raise typer.Exit(1)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)


def terminate_process(process: Optional[subprocess.Popen[str]]) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def cleanup_partial(path: Path) -> Optional[str]:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return f"Could not remove partial output {path}: {error}"
    return None


def discard_not_smaller(output: Path, source_size: int) -> Optional[int]:
    output_size = output.stat().st_size
    if output_size < source_size:
        return None
    cleanup_error = cleanup_partial(output)
    if cleanup_error:
        raise BrakeSmithError(cleanup_error)
    return output_size


def delete_replaced_source(source: Path, destination: Path) -> None:
    try:
        source.unlink()
    except OSError as error:
        raise BrakeSmithError(
            f"Validated output is safe at {destination}, but source could not be deleted: {error}"
        ) from error


def write_failure_log(destination: Path, lines: list[str]) -> Path:
    candidate = destination.with_name(f"{destination.name}.handbrake.log")
    counter = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.handbrake.{counter}.log")
        counter += 1
    candidate.write_text("".join(lines), encoding="utf-8")
    return candidate


@app.command(name="run")
def run_batch(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    max_files: Optional[int] = typer.Option(
        None, min=1, help="Maximum conversion candidates; interactive runs ask when omitted."
    ),
    audio: str = typer.Option("eng,fra", help="Audio languages to keep (ISO codes or names)."),
    subtitles: str = typer.Option("eng,fra", help="Subtitle languages to keep."),
    unknown_audio: str = typer.Option("ask", help="Unlabelled audio tracks: ask, keep, or drop."),
    unknown_subtitles: str = typer.Option(
        "ask", help="Unlabelled subtitle tracks: ask, keep, or drop."
    ),
    keep_commentary: bool = typer.Option(False, help="Keep commentary tracks."),
    forced_subtitles_only: bool = typer.Option(False, help="Keep only forced requested subtitles."),
    exclude_titles: str = typer.Option(
        "commentary,description", help="Track-title fragments to exclude."
    ),
    keep_original: bool = typer.Option(
        False,
        "--keep-original/--no-keep-original",
        help="Also keep detected original-language audio; opt-in.",
    ),
    original_language: Optional[str] = typer.Option(
        None, help="Fallback original language for files lacking metadata."
    ),
    quality: float = typer.Option(
        18.0, min=0, max=51, help="x265 constant quality; lower is larger/better."
    ),
    preset: str = typer.Option("slow", help="x265 encoder preset."),
    bit_depth: int = typer.Option(10, min=8, max=12, help="x265 output bit depth."),
    tune: Optional[str] = typer.Option(None, help="x265 tune."),
    encoder_profile: Optional[str] = typer.Option(None, help="x265 profile."),
    encoder_level: Optional[str] = typer.Option(None, help="x265 level."),
    crop: str = typer.Option("auto", help="Crop policy: auto or none."),
    deinterlace: str = typer.Option("auto", help="auto, off, decomb, or yadif."),
    lossless: bool = typer.Option(False, help="x265 lossless mode; output may be huge."),
    output_directory: Optional[Path] = typer.Option(
        None, help="Mirror results under another directory."
    ),
    replace_source: bool = typer.Option(
        False,
        "--replace-source/--keep-source",
        help="Replace each source only when its validated HEVC output is smaller.",
    ),
    include_hevc: bool = typer.Option(False, help="Reprocess files already encoded as HEVC."),
    allow_no_audio: bool = typer.Option(
        False, help="Allow output without audio even when source has audio."
    ),
    invalid_existing: str = typer.Option(
        "fail", help="Invalid existing output: fail or quarantine."
    ),
    stale_partial: str = typer.Option(
        "fail", help="Stale partial output: fail, quarantine, or delete."
    ),
    summary: Optional[Path] = typer.Option(None, help="Write atomic JSON batch summary."),
    force_summary: bool = typer.Option(False, help="Replace only an existing summary report."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    workers: int = typer.Option(2, min=1, max=32, help="Concurrent metadata probes."),
    probe_timeout: float = typer.Option(60, min=1, help="Seconds allowed per metadata probe."),
    cache: Optional[Path] = typer.Option(None, "--cache-file", help="Local probe-cache path."),
    use_cache: bool = typer.Option(True, "--cache/--no-cache", help="Use local probe cache."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip final confirmation."),
    non_interactive: bool = typer.Option(
        False, help="Forbid all prompts; unresolved choices fail."
    ),
    profile: Optional[str] = typer.Option(None, help="Named TOML profile."),
    config: Optional[Path] = typer.Option(None, help="TOML config path."),
    handbrake: Optional[Path] = typer.Option(None, help="Path to HandBrakeCLI."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """Review and execute a safe batch conversion."""
    try:
        profile_settings = load_profile(profile, config)
        audio = str(prefer_profile(profile_settings, "audio", audio, "eng,fra"))
        subtitles = str(prefer_profile(profile_settings, "subtitles", subtitles, "eng,fra"))
        unknown_audio = str(prefer_profile(profile_settings, "unknown_audio", unknown_audio, "ask"))
        unknown_subtitles = str(
            prefer_profile(profile_settings, "unknown_subtitles", unknown_subtitles, "ask")
        )
        quality = float(prefer_profile(profile_settings, "quality", quality, 18.0))
        preset = str(prefer_profile(profile_settings, "preset", preset, "slow"))
        bit_depth = int(prefer_profile(profile_settings, "bit_depth", bit_depth, 10))
        crop = str(prefer_profile(profile_settings, "crop", crop, "auto"))
        deinterlace = str(prefer_profile(profile_settings, "deinterlace", deinterlace, "auto"))
        workers = int(prefer_profile(profile_settings, "workers", workers, 2))
        probe_timeout = float(prefer_profile(profile_settings, "probe_timeout", probe_timeout, 60))
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    executable = find_executable("HandBrakeCLI", handbrake)
    ffprobe_executable = find_executable("ffprobe", ffprobe)
    if not executable or not ffprobe_executable:
        console.print("[red]Error:[/] HandBrakeCLI or ffprobe not found. Run `brakesmith doctor`.")
        raise typer.Exit(2)
    if invalid_existing not in {"fail", "quarantine"}:
        console.print("[red]Error:[/] --invalid-existing must be fail or quarantine")
        raise typer.Exit(2)
    if stale_partial not in {"fail", "quarantine", "delete"}:
        console.print("[red]Error:[/] --stale-partial must be fail, quarantine, or delete")
        raise typer.Exit(2)
    try:
        quality, preset = reconcile_quality_profile(quality, preset, non_interactive)
        max_files = reconcile_max_files(max_files, non_interactive)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    try:
        items = limit_proposed_files(
            [
                m
                for m in inspect(
                    directory,
                    depth,
                    ffprobe,
                    extensions,
                    workers,
                    probe_timeout,
                    cache,
                    use_cache,
                )
                if m.should_convert or include_hevc
            ],
            max_files,
        )
        if not items:
            return
        (
            audio_languages,
            subtitle_languages,
            unknown_audio_choice,
            unknown_subtitle_choice,
        ) = reconcile_languages(
            items,
            audio,
            subtitles,
            non_interactive,
            choose_unknown_audio=unknown_audio == "ask",
            choose_unknown_subtitles=unknown_subtitles == "ask",
        )
        if unknown_audio_choice is not None:
            unknown_audio = "keep" if unknown_audio_choice else "drop"
        if unknown_subtitle_choice is not None:
            unknown_subtitles = "keep" if unknown_subtitle_choice else "drop"
        originals = reconcile_original(items, keep_original, original_language, non_interactive)
        extra_audio = reconcile_unknown_tracks(items, "audio", unknown_audio, non_interactive)
        extra_subtitles = reconcile_unknown_tracks(
            items, "subtitles", unknown_subtitles, non_interactive
        )
        render(items, directory)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    excluded_titles = [value.strip() for value in exclude_titles.split(",") if value.strip()]
    selected_audio: dict[Path, list[int]] = {}
    selected_subtitles: dict[Path, list[int]] = {}
    for item in items:
        kept_audio = sorted(
            set(
                select_tracks(
                    item.audio,
                    audio_languages,
                    originals[item.path],
                    keep_commentary=keep_commentary,
                    exclude_titles=excluded_titles,
                    fallback_default=True,
                )
            )
            | extra_audio[item.path]
        )
        kept_subs = sorted(
            set(
                select_tracks(
                    item.subtitles,
                    subtitle_languages,
                    keep_commentary=keep_commentary,
                    forced_only=forced_subtitles_only,
                    exclude_titles=excluded_titles,
                    fallback_default=True,
                )
            )
            | extra_subtitles[item.path]
        )
        if item.audio and not kept_audio and not allow_no_audio:
            console.print(
                f"[red]Error:[/] No audio selected for {item.path}; choose a language, keep an "
                "unlabelled track, or pass --allow-no-audio."
            )
            raise typer.Exit(2)
        selected_audio[item.path] = kept_audio
        selected_subtitles[item.path] = kept_subs
        console.print(
            f"{item.path.name}: audio {kept_audio or 'none'}, subtitles {kept_subs or 'none'}, original {originals[item.path] or 'unknown'}"
        )
        for warning in fidelity_warnings(item):
            console.print(f"[yellow]Warning:[/] {item.path.name}: {warning}")
    destinations = {
        item.path: (
            replacement_output_path(item.path, output_directory, directory)
            if replace_source
            else output_path(item.path, output_directory, directory)
        )
        for item in items
    }
    try:
        validate_destinations([(item.path, destinations[item.path]) for item in items])
        snapshots = {item.path: snapshot_source(item.path) for item in items}
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    action = (
        "Only smaller validated outputs will replace and permanently delete their sources"
        if replace_source
        else "Originals will remain untouched"
    )
    if not yes and not Confirm.ask(f"Convert {len(items)} file(s)? {action}", default=False):
        console.print("Cancelled. No files changed.")
        return

    try:
        for item in items:
            preflight_destination(destinations[item.path], item.size)
            ensure_source_unchanged(snapshots[item.path])
    except BrakeSmithError as error:
        console.print(f"[red]Preflight failed:[/] {error}")
        raise typer.Exit(2)

    progress_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    failures = 0
    completed = 0
    skipped = 0
    summary_entries: list[dict[str, object]] = []
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_cancel(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_cancel)
    try:
        with Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            total_task = progress.add_task("Transcoding files", total=len(items))
            for file_number, item in enumerate(items, start=1):
                destination = destinations[item.path]
                ensure_source_unchanged(snapshots[item.path])
                partial = destination.with_name(destination.name + ".part")
                if destination.exists():
                    try:
                        validate_output(
                            destination,
                            ffprobe_executable,
                            item.duration,
                            len(selected_audio[item.path]),
                            len(selected_subtitles[item.path]),
                            item.chapters,
                        )
                    except BrakeSmithError as error:
                        if invalid_existing == "quarantine":
                            quarantined = quarantine_file(destination, "invalid")
                            console.print(f"[yellow]Quarantined:[/] {quarantined} ({error})")
                        else:
                            failures += 1
                            console.print(f"[red]Failed:[/] Existing output is invalid: {error}")
                            summary_entries.append(
                                {
                                    "source": str(item.path),
                                    "output": str(destination),
                                    "status": "failed",
                                    "error": str(error),
                                }
                            )
                            progress.update(total_task, completed=file_number)
                            continue
                    else:
                        if replace_source:
                            try:
                                discarded_size = discard_not_smaller(
                                    destination, snapshots[item.path].size
                                )
                                if discarded_size is None:
                                    delete_replaced_source(item.path, destination)
                            except BrakeSmithError as error:
                                failures += 1
                                console.print(f"[red]Failed:[/] {error}")
                                summary_entries.append(
                                    {
                                        "source": str(item.path),
                                        "output": str(destination),
                                        "status": "failed-source-delete",
                                        "error": str(error),
                                    }
                                )
                            else:
                                if discarded_size is not None:
                                    skipped += 1
                                    console.print(
                                        f"[yellow]Kept source:[/] existing output was not smaller "
                                        f"({discarded_size} >= {snapshots[item.path].size} bytes)"
                                    )
                                    summary_entries.append(
                                        {
                                            "source": str(item.path),
                                            "output": None,
                                            "status": "kept-source-not-smaller",
                                            "source_bytes": snapshots[item.path].size,
                                            "output_bytes": discarded_size,
                                        }
                                    )
                                else:
                                    completed += 1
                                    console.print(
                                        f"[green]Replaced:[/] {item.path} → {destination}"
                                    )
                                    summary_entries.append(
                                        {
                                            "source": str(item.path),
                                            "output": str(destination),
                                            "status": "completed-existing",
                                            "source_deleted": True,
                                        }
                                    )
                        else:
                            console.print(f"[yellow]Skip:[/] Valid output exists: {destination}")
                            skipped += 1
                            summary_entries.append(
                                {
                                    "source": str(item.path),
                                    "output": str(destination),
                                    "status": "skipped-valid",
                                }
                            )
                        progress.update(total_task, completed=file_number)
                        continue
                if partial.exists():
                    try:
                        validate_output(
                            partial,
                            ffprobe_executable,
                            item.duration,
                            len(selected_audio[item.path]),
                            len(selected_subtitles[item.path]),
                            item.chapters,
                        )
                        classification = "valid"
                    except BrakeSmithError:
                        classification = "invalid"
                    if stale_partial == "fail":
                        failures += 1
                        message = f"Stale {classification} partial requires --stale-partial quarantine or delete: {partial}"
                        console.print(f"[red]Failed:[/] {message}")
                        summary_entries.append(
                            {
                                "source": str(item.path),
                                "output": str(destination),
                                "status": "failed",
                                "error": message,
                            }
                        )
                        progress.update(total_task, completed=file_number)
                        continue
                    if stale_partial == "quarantine":
                        quarantined = quarantine_file(partial, f"stale-{classification}")
                        console.print(f"[yellow]Quarantined stale partial:[/] {quarantined}")
                    else:
                        cleanup_error = cleanup_partial(partial)
                        if cleanup_error:
                            raise BrakeSmithError(cleanup_error)
                        console.print(f"[yellow]Deleted stale partial:[/] {partial}")
                file_task = progress.add_task(
                    f"[{file_number}/{len(items)}] {item.path.name}", total=100
                )
                command = handbrake_command(
                    executable,
                    item,
                    partial,
                    audio_languages,
                    subtitle_languages,
                    originals[item.path],
                    quality,
                    preset,
                    extra_audio[item.path],
                    extra_subtitles[item.path],
                    bit_depth,
                    tune,
                    encoder_profile,
                    encoder_level,
                    crop,
                    deinterlace,
                    lossless,
                    selected_audio[item.path],
                    selected_subtitles[item.path],
                )
                process: Optional[subprocess.Popen[str]] = None
                diagnostics: list[str] = []
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                    )
                    assert process.stdout is not None
                    for line in process.stdout:
                        diagnostics.append(line)
                        match = progress_pattern.search(line)
                        if match:
                            percent = min(float(match.group(1)), 100)
                            progress.update(file_task, completed=percent)
                            progress.update(
                                total_task,
                                completed=(file_number - 1) + percent / 100,
                            )
                    if process.wait() != 0:
                        raise BrakeSmithError(f"HandBrake failed for {item.path.name}")
                    ensure_source_unchanged(snapshots[item.path])
                    validate_output(
                        partial,
                        ffprobe_executable,
                        item.duration,
                        len(selected_audio[item.path]),
                        len(selected_subtitles[item.path]),
                        item.chapters,
                    )
                    discarded_size = (
                        discard_not_smaller(partial, snapshots[item.path].size)
                        if replace_source
                        else None
                    )
                    if discarded_size is not None:
                        skipped += 1
                        console.print(
                            f"[yellow]Kept source:[/] output was not smaller "
                            f"({discarded_size} >= {snapshots[item.path].size} bytes): {item.path}"
                        )
                        summary_entries.append(
                            {
                                "source": str(item.path),
                                "output": None,
                                "status": "kept-source-not-smaller",
                                "source_bytes": snapshots[item.path].size,
                                "output_bytes": discarded_size,
                            }
                        )
                        progress.update(file_task, completed=100)
                        continue
                    partial.replace(destination)
                    if replace_source:
                        delete_replaced_source(item.path, destination)
                    completed += 1
                    summary_entries.append(
                        {
                            "source": str(item.path),
                            "output": str(destination),
                            "status": "completed",
                            "source_deleted": replace_source,
                        }
                    )
                    progress.update(file_task, completed=100)
                except KeyboardInterrupt:
                    terminate_process(process)
                    cleanup_error = cleanup_partial(partial)
                    console.print(
                        "\n[yellow]Cancelled.[/] Current partial cleanup attempted; completed "
                        "replacements remain."
                    )
                    if cleanup_error:
                        console.print(f"[red]Warning:[/] {cleanup_error}")
                    summary_entries.append(
                        {
                            "source": str(item.path),
                            "output": str(destination),
                            "status": "cancelled",
                        }
                    )
                    if summary:
                        try:
                            atomic_write_json(summary, summary_entries, force_summary)
                        except BrakeSmithError as error:
                            console.print(f"[red]Warning:[/] {error}")
                    raise typer.Exit(130)
                except Exception as error:  # noqa: BLE001 - cleanup must cover filesystem failures
                    terminate_process(process)
                    quarantined: Optional[Path] = None
                    if partial.exists():
                        try:
                            quarantined = quarantine_file(partial, "invalid")
                        except BrakeSmithError:
                            quarantined = None
                    cleanup_error = cleanup_partial(partial) if partial.exists() else None
                    failures += 1
                    console.print(f"[red]Failed:[/] {error}")
                    log_path: Optional[Path] = None
                    if diagnostics:
                        try:
                            log_path = write_failure_log(destination, diagnostics)
                            console.print(f"[yellow]HandBrake log:[/] {log_path}")
                        except OSError as log_error:
                            console.print(
                                f"[red]Warning:[/] Cannot save HandBrake log: {log_error}"
                            )
                    summary_entries.append(
                        {
                            "source": str(item.path),
                            "output": str(destination),
                            "status": "failed",
                            "error": str(error),
                            "quarantined": str(quarantined) if quarantined else None,
                            "log": str(log_path) if log_path else None,
                        }
                    )
                    if cleanup_error:
                        console.print(f"[red]Warning:[/] {cleanup_error}")
                finally:
                    progress.remove_task(file_task)
                    progress.update(total_task, completed=file_number)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    source_result = (
        "Only smaller validated outputs replaced sources."
        if replace_source
        else "Originals untouched."
    )
    console.print(
        f"[green]Done:[/] {completed} completed, {skipped} skipped, {failures} failed. "
        f"{source_result}"
    )
    if summary:
        try:
            atomic_write_json(summary, summary_entries, force_summary)
            console.print(f"[green]Summary:[/] {summary.resolve()}")
        except BrakeSmithError as error:
            console.print(f"[red]Summary failed:[/] {error}")
            failures += 1
    if failures:
        raise typer.Exit(1)
