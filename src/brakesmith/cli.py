from __future__ import annotations

import csv
import io
import json
import os
import re
import signal
import subprocess
from collections import Counter, deque
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from hashlib import sha256
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
from .config import load_format_settings, load_profile, prefer_profile, save_format_settings
from .core import (
    FORMAT_PRESETS,
    BrakeSmithError,
    FormatSettings,
    MediaFile,
    ProbeCache,
    ScanInterrupted,
    SourceSnapshot,
    atomic_write_json,
    discover,
    ensure_source_unchanged,
    expected_audio_track_count,
    fidelity_warnings,
    find_executable,
    handbrake_command,
    normalize_languages,
    output_path,
    preflight_destination,
    probe,
    quarantine_file,
    replacement_output_path,
    resolve_format_settings,
    select_tracks,
    snapshot_source,
    text_subtitle_tracks,
    validate_destinations,
    validate_output,
)
from .failures import FAILURE_TYPES, FailureStore
from .plans import load_plan, load_state, write_plan, write_state

app = typer.Typer(
    help="Forge a safe, reviewed batch of H.265 files with HandBrakeCLI.", no_args_is_help=True
)
failures_app = typer.Typer(help="Manage files blocked after failed or unhelpful conversions.")
app.add_typer(failures_app, name="failures")
machine_width = 1000 if os.environ.get("BRAKESMITH_EVENTS") == "1" else None
console = Console(width=machine_width)
error_console = Console(stderr=True, width=machine_width)
MACHINE_EVENT_PREFIX = "BRAKESMITH_EVENT "


def emit_machine_event(event: str, **payload: object) -> None:
    """Emit one structured progress event when a machine client requested it."""
    if os.environ.get("BRAKESMITH_EVENTS") != "1":
        return
    typer.echo(
        f"{MACHINE_EVENT_PREFIX}{json.dumps({'event': event, **payload}, default=str)}",
        err=True,
    )


def cancellation_requested() -> bool:
    marker = os.environ.get("BRAKESMITH_CANCEL_FILE")
    return bool(marker and Path(marker).is_file())


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


@app.command(hidden=True)
def bridge(
    protocol: int = typer.Option(1, help="Machine protocol version."),
) -> None:
    """Serve one versioned desktop request from standard input."""
    from .bridge import run_bridge

    run_bridge(protocol)


def inspect(
    root: Path,
    depth: int,
    ffprobe_path: Optional[Path],
    extensions: str = "",
    workers: int = 2,
    probe_timeout: float = 60,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    retry_blocked: bool = False,
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
            if directories == 1 or directories % 20 == 0:
                emit_machine_event(
                    "progress", phase="discover", directories=directories, discovered=files
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
        emit_machine_event(
            "progress",
            phase="discover",
            directories=None,
            discovered=len(paths),
            finished=True,
        )
    media: list[MediaFile] = []
    errors = list(traversal_errors)
    cache = ProbeCache(cache_path) if use_cache else None
    outcome_store = FailureStore()
    probe_policy = policy_digest({"ffprobe": ffprobe, "timeout": probe_timeout})
    pending: list[Path] = []
    cache_hits = 0
    blocked_probes = 0
    for path in paths:
        previous = outcome_store.active(path, probe_policy)
        if (
            not retry_blocked
            and previous
            and previous.get("type") == "probe"
            and FailureStore.blocks(previous)
        ):
            blocked_probes += 1
            continue
        cached = cache.get(path) if cache else None
        if cached:
            media.append(cached)
            cache_hits += 1
            try:
                outcome_store.resolve_probe(path)
            except BrakeSmithError as registry_error:
                errors.append(str(registry_error))
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
    analyzed = cache_hits + blocked_probes
    try:
        with progress:
            task = progress.add_task(
                f"Analyzing metadata ({cache_hits} cached)",
                total=len(paths),
                completed=analyzed,
            )
            for future in as_completed(futures):
                path = futures[future]
                try:
                    item = future.result()
                    media.append(item)
                    if cache:
                        cache.put(item)
                    try:
                        outcome_store.resolve_probe(path)
                    except BrakeSmithError as registry_error:
                        errors.append(str(registry_error))
                except BrakeSmithError as error:
                    errors.append(str(error))
                    try:
                        outcome_store.record(path, "probe", str(error), policy_hash=probe_policy)
                    except BrakeSmithError as registry_error:
                        errors.append(str(registry_error))
                analyzed += 1
                progress.advance(task)
                emit_machine_event(
                    "progress",
                    phase="probe",
                    completed=analyzed,
                    total=len(paths),
                    source=str(path),
                )
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
        emit_machine_event("warning", phase="scan", message=error)
        error_console.print(f"[yellow]Warning:[/] {error}")
    error_console.print(
        f"[dim]Inspected {len(media)}/{len(paths)} files; {cache_hits} cache hits; "
        f"{blocked_probes} blocked probe(s); {len(errors)} warning(s).[/]"
    )
    return sorted(media, key=lambda item: str(item.path).lower())


def policy_digest(settings: dict[str, object]) -> str:
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def outcome_reason(record: dict[str, object]) -> str:
    kind = str(record.get("type", "previous attempt failed"))
    error = record.get("error")
    return f"{kind}: {error}" if error else kind


def render(
    media: list[MediaFile],
    root: Path,
    view: str = "detailed",
    states: Optional[dict[Path, dict[str, object]]] = None,
) -> None:
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
    states = states or {}
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
        state = states.get(item.path)
        if not item.should_convert:
            status = "[green]HEVC[/]"
            reason = "already HEVC"
        elif state and state.get("outcome") == "success":
            status = "[green]done[/]"
            reason = str(state.get("result", "successful output exists"))
        elif FailureStore.blocks(state):
            status = "[yellow]blocked[/]"
            reason = outcome_reason(state)
        else:
            status = "[cyan]convert[/]"
            reason = "video codec is not HEVC"
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
    blocked = sum(
        item.should_convert and FailureStore.blocks(states.get(item.path)) for item in media
    )
    done = sum(
        not item.should_convert or states.get(item.path, {}).get("outcome") == "success"
        for item in media
    )
    ready = len(media) - blocked - done
    console.print(
        f"[bold]{len(media)}[/] video(s), [bold cyan]{ready}[/] ready, "
        f"[bold green]{done}[/] done/not required, [bold yellow]{blocked}[/] blocked"
    )


def display(
    media: list[MediaFile],
    root: Path,
    view: str,
    pager: bool = False,
    states: Optional[dict[Path, dict[str, object]]] = None,
) -> None:
    if pager and console.is_terminal:
        with console.pager(styles=True):
            render(media, root, view, states)
    else:
        render(media, root, view, states)


def media_payload(item: MediaFile, state: Optional[dict[str, object]] = None) -> dict[str, object]:
    payload = {
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
    if not item.should_convert:
        payload["transcode_status"] = "not-required"
    elif state and state.get("outcome") == "success":
        payload["transcode_status"] = "success"
    elif FailureStore.blocks(state):
        payload["transcode_status"] = "blocked"
        payload["blocked_reason"] = state.get("type")
    else:
        payload["transcode_status"] = "ready"
    return payload


def write_candidates_report(
    items: list[MediaFile],
    output: Path,
    force: bool = False,
    states: Optional[dict[Path, dict[str, object]]] = None,
) -> None:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise BrakeSmithError(f"Report exists: {output}; pass --force to replace it")
    suffix = output.suffix.lower()
    if suffix == ".json":
        content = (
            json.dumps(
                [media_payload(item, (states or {}).get(item.path)) for item in items], indent=2
            )
            + "\n"
        )
    elif suffix == ".csv":
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "path",
                "codec",
                "duration_seconds",
                "size_bytes",
                "audio",
                "subtitles",
                "transcode_status",
                "blocked_reason",
            ]
        )
        for item in items:
            state = (states or {}).get(item.path)
            payload = media_payload(item, state)
            writer.writerow(
                [
                    item.path,
                    item.codec,
                    item.duration,
                    item.size,
                    ",".join(track.language for track in item.audio),
                    ",".join(track.language for track in item.subtitles),
                    payload["transcode_status"],
                    payload.get("blocked_reason", ""),
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
        store = FailureStore()
    except ScanInterrupted as error:
        error_console.print(f"[yellow]{error}[/]")
        raise typer.Exit(130)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    states = {
        item.path: record for item in items if (record := store.active(item.path)) is not None
    }
    if json_output:
        payload = [media_payload(item, states.get(item.path)) for item in items]
        typer.echo(json.dumps(payload, indent=2))
    else:
        display(items, directory, view, pager, states)


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
    include_blocked: bool = typer.Option(
        False, help="Include unchanged sources from remembered non-candidates."
    ),
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
        store = FailureStore()
        items = exclude_remembered_failures(items, store, include_blocked)
        states = {
            item.path: record for item in items if (record := store.active(item.path)) is not None
        }
        if output:
            write_candidates_report(items, output, force, states)
            console.print(f"[green]Saved:[/] {len(items)} conversion candidate(s) to {output}")
        else:
            display(items, directory, view, pager, states)
    except ScanInterrupted as error:
        if output:
            partial = output.with_name(f"{output.stem}.partial{output.suffix}")
            try:
                partial_items = [item for item in error.items if item.should_convert]
                store = FailureStore()
                partial_items = exclude_remembered_failures(partial_items, store, include_blocked)
                states = {
                    item.path: record
                    for item in partial_items
                    if (record := store.active(item.path)) is not None
                }
                write_candidates_report(partial_items, partial, force=True, states=states)
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
    ffmpeg: Optional[Path] = typer.Option(None, help="Path to ffmpeg."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Check required tools without changing files."""
    required = {
        "HandBrakeCLI": find_executable("HandBrakeCLI", handbrake),
        "ffprobe": find_executable("ffprobe", ffprobe),
    }
    optional_ffmpeg = find_executable("ffmpeg", ffmpeg)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "version": __version__,
                    "healthy": all(required.values()),
                    "tools": {
                        "handbrake": required["HandBrakeCLI"],
                        "ffprobe": required["ffprobe"],
                        "ffmpeg": optional_ffmpeg,
                    },
                },
                indent=2,
            )
        )
    else:
        for name, path in required.items():
            console.print(f"[green]✓[/] {name}: {path}" if path else f"[red]✗[/] {name}: not found")
        console.print(
            f"[green]✓[/] ffmpeg (full health checks): {optional_ffmpeg}"
            if optional_ffmpeg
            else "[yellow]○[/] ffmpeg: optional; full health checks unavailable"
        )
    if not all(required.values()):
        if not json_output:
            console.print("Install HandBrakeCLI and ffprobe, or pass explicit paths.")
        raise typer.Exit(1)


def check_media_health(
    path: Path,
    full: bool,
    ffprobe_executable: str | None,
    ffmpeg_executable: str | None,
    timeout: float,
) -> dict[str, object]:
    mode = "full" if full else "quick"
    try:
        if full:
            if not ffmpeg_executable:
                raise BrakeSmithError("ffmpeg not found")
            result = subprocess.run(
                [
                    ffmpeg_executable,
                    "-v",
                    "error",
                    "-xerror",
                    "-nostdin",
                    "-i",
                    str(path),
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout or None,
                check=False,
            )
            if result.returncode:
                details = (result.stderr or result.stdout).strip()[-2000:]
                raise BrakeSmithError(details or f"ffmpeg exited with {result.returncode}")
        else:
            if not ffprobe_executable:
                raise BrakeSmithError("ffprobe not found")
            probe(path, ffprobe_executable, timeout or None)
    except subprocess.TimeoutExpired:
        return {"path": str(path), "mode": mode, "status": "error", "error": "timed out"}
    except (BrakeSmithError, OSError) as error:
        return {"path": str(path), "mode": mode, "status": "error", "error": str(error)}
    return {"path": str(path), "mode": mode, "status": "healthy", "error": None}


@app.command(name="health")
def health_check(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    full: bool = typer.Option(False, "--full/--quick", help="Decode all frames or check headers."),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    timeout: float = typer.Option(0, min=0, help="Seconds per file; 0 disables timeout."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions."),
    selection: Optional[Path] = typer.Option(
        None, exists=True, dir_okay=False, help="JSON list of exact source paths to check."
    ),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
    ffmpeg: Optional[Path] = typer.Option(None, help="Path to ffmpeg."),
) -> None:
    """Check media headers or decode all video and audio frames."""
    ffprobe_executable = find_executable("ffprobe", ffprobe) if not full else None
    ffmpeg_executable = find_executable("ffmpeg", ffmpeg) if full else None
    if (full and not ffmpeg_executable) or (not full and not ffprobe_executable):
        tool = "ffmpeg" if full else "ffprobe"
        console.print(f"[red]Error:[/] {tool} not found.")
        raise typer.Exit(2)
    traversal_errors: list[str] = []
    paths = discover(
        directory,
        None if depth < 0 else depth,
        extensions.split(",") if extensions else (),
        errors=traversal_errors,
    )
    try:
        paths = select_exact_paths(paths, directory, selection)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    results: list[dict[str, object]] = []
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=error_console,
        disable=json_output,
    ) as progress:
        task = progress.add_task("Checking media health", total=len(paths))
        try:
            for path in paths:
                emit_machine_event(
                    "item",
                    phase="health",
                    source=str(path),
                    completed=len(results),
                    total=len(paths),
                )
                results.append(
                    check_media_health(path, full, ffprobe_executable, ffmpeg_executable, timeout)
                )
                progress.advance(task)
                emit_machine_event(
                    "progress",
                    phase="health",
                    source=str(path),
                    completed=len(results),
                    total=len(paths),
                )
        except KeyboardInterrupt:
            error_console.print("[yellow]Health check cancelled.[/]")
            raise typer.Exit(130)
    for error in traversal_errors:
        error_console.print(f"[yellow]Warning:[/] {error}")
    if json_output:
        typer.echo(json.dumps(results, indent=2))
    else:
        table = Table(title=f"BrakeSmith health · {directory}", show_lines=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Mode", no_wrap=True)
        table.add_column("File", overflow="fold", ratio=3)
        table.add_column("Details", overflow="fold", ratio=2)
        for result in results:
            status = "[green]healthy[/]" if result["status"] == "healthy" else "[red]error[/]"
            table.add_row(
                status,
                str(result["mode"]),
                str(result["path"]),
                str(result["error"] or "—"),
            )
        console.print(table)
        errors = sum(result["status"] == "error" for result in results)
        console.print(f"{len(results) - errors} healthy, {errors} error(s).")
    if any(result["status"] == "error" for result in results):
        raise typer.Exit(1)


@failures_app.command(name="list")
def list_failures(
    kind: Optional[str] = typer.Option(None, "--type", help="Filter by failure type."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """List remembered source files that normal runs will not propose."""
    if kind and kind not in FAILURE_TYPES:
        console.print(f"[red]Error:[/] --type must be one of: {', '.join(sorted(FAILURE_TYPES))}")
        raise typer.Exit(2)
    try:
        store = FailureStore()
        records = [record for record in store.records(kind) if FailureStore.blocks(record)]
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    if json_output:
        payload = []
        for record in records:
            source = Path(str(record["source"]))
            payload.append({**record, "active": bool(store.active(source))})
        typer.echo(json.dumps(payload, indent=2))
        return
    if not records:
        console.print("No remembered non-candidates.")
        return
    table = Table(title=f"BrakeSmith failures · {store.path}", show_lines=True)
    table.add_column("Source", overflow="fold", ratio=3)
    table.add_column("Reason", no_wrap=True)
    table.add_column("Active", no_wrap=True)
    table.add_column("Details", overflow="fold", ratio=2)
    for record in records:
        source = Path(str(record["source"]))
        table.add_row(
            f"{source.name}\n{source.parent}",
            str(record.get("type", "unknown")),
            "yes" if store.active(source) else "no",
            f"Recorded: {record.get('failed_at', '')}\nDetails: {record.get('error', '')}\n"
            f"Log: {record.get('log') or '—'}",
        )
    console.print(table)


@failures_app.command(name="clear")
def clear_failures(
    kind: Optional[str] = typer.Option(None, "--type", help="Clear only one failure type."),
    logs_only: bool = typer.Option(False, help="Delete logs but keep skip records."),
    keep_logs: bool = typer.Option(False, help="Clear records but retain log files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Clear remembered non-candidates and their centralized logs."""
    if kind and kind not in FAILURE_TYPES:
        console.print(f"[red]Error:[/] --type must be one of: {', '.join(sorted(FAILURE_TYPES))}")
        raise typer.Exit(2)
    if logs_only and keep_logs:
        console.print("[red]Error:[/] --logs-only and --keep-logs cannot be combined")
        raise typer.Exit(2)
    try:
        store = FailureStore()
        count = sum(FailureStore.blocks(record) for record in store.records(kind))
        pattern = f"*-{kind}.log" if kind else "*.log"
        log_count = sum(1 for _ in store.logs.glob(pattern))
        if not count and not (logs_only and log_count):
            console.print("No matching failures.")
            return
        target = (
            f"{log_count} log(s)"
            if logs_only
            else f"{count} record(s)" + (f" of type {kind}" if kind else "")
        )
        if not yes and not Confirm.ask(f"Clear {target}?", default=False):
            console.print("Cancelled.")
            return
        records_removed, logs_removed = store.clear(kind, logs_only, keep_logs)
    except (BrakeSmithError, OSError) as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    console.print(f"[green]Cleared:[/] {records_removed} record(s), {logs_removed} log(s).")


@failures_app.command(name="forget")
def forget_failures(
    sources: list[Path] = typer.Argument(..., help="Source paths to propose again."),
    keep_logs: bool = typer.Option(False, help="Retain diagnostic log files."),
) -> None:
    """Forget specific sources so later runs can propose them again."""
    try:
        records_removed, logs_removed = FailureStore().forget(sources, keep_logs)
    except (BrakeSmithError, OSError) as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    console.print(f"[green]Forgot:[/] {records_removed} source(s), {logs_removed} log(s).")


@failures_app.command(name="prune")
def prune_failures() -> None:
    """Remove records for missing or changed sources and orphan logs."""
    try:
        records_removed, logs_removed = FailureStore().prune()
    except (BrakeSmithError, OSError) as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    console.print(
        f"[green]Pruned:[/] {records_removed} stale record(s), {logs_removed} orphan log(s)."
    )


@failures_app.command(name="path")
def failure_path() -> None:
    """Show failure registry and log directory."""
    store = FailureStore()
    console.print(f"Registry: {store.path}\nLogs: {store.logs}")


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


@app.command(name="retry")
def retry_outcomes(
    sources: list[Path] = typer.Argument(None, help="Specific source paths to retry."),
    root: Optional[Path] = typer.Option(None, help="Retry blocked files under this directory."),
    kind: Optional[str] = typer.Option(None, "--type", help="Retry only this failure type."),
) -> None:
    """Release selected blocked files for the next run or plan."""
    if kind and kind not in FAILURE_TYPES:
        console.print(f"[red]Error:[/] --type must be one of: {', '.join(sorted(FAILURE_TYPES))}")
        raise typer.Exit(2)
    if not sources and root is None and kind is None:
        console.print("[red]Error:[/] Select source paths, --root, or --type.")
        raise typer.Exit(2)
    try:
        store = FailureStore()
        wanted = {store.key(source) for source in (sources or [])}
        selected = []
        for record in store.records(kind):
            source = Path(str(record["source"]))
            if not FailureStore.blocks(record):
                continue
            if wanted and store.key(source) not in wanted:
                continue
            if root is not None and not path_is_under(source, root):
                continue
            selected.append(source)
        released = store.release(selected)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    console.print(f"[green]Ready to retry:[/] {released} source(s). Run `brakesmith run`.")


@app.command(name="history")
def show_history(
    root: Path = typer.Argument(Path("."), help="Show records under this directory."),
    kind: Optional[str] = typer.Option(None, "--type", help="Filter attempt type."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
) -> None:
    """Show bounded transcode attempt history."""
    valid_types = {*FAILURE_TYPES, "success"}
    if kind and kind not in valid_types:
        console.print(f"[red]Error:[/] --type must be one of: {', '.join(sorted(valid_types))}")
        raise typer.Exit(2)
    try:
        store = FailureStore()
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    payload = []
    for record in store.records():
        source = Path(str(record["source"]))
        if not path_is_under(source, root):
            continue
        attempts = record.get("history")
        if not isinstance(attempts, list):
            attempts = [
                {
                    "recorded_at": record.get("recorded_at", record.get("failed_at")),
                    "outcome": record.get("outcome", "blocked"),
                    "type": record.get("type"),
                    "error": record.get("error"),
                }
            ]
        for attempt in attempts:
            if not isinstance(attempt, dict) or (kind and attempt.get("type") != kind):
                continue
            payload.append({"source": str(source), **attempt})
    payload.sort(key=lambda item: str(item.get("recorded_at", "")), reverse=True)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not payload:
        console.print("No matching history.")
        return
    table = Table(title=f"BrakeSmith history · {root.resolve()}", show_lines=True)
    table.add_column("Time", no_wrap=True)
    table.add_column("Outcome", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Source", overflow="fold")
    table.add_column("Details", overflow="fold")
    for attempt in payload:
        table.add_row(
            str(attempt.get("recorded_at", "")),
            str(attempt.get("outcome", "")),
            str(attempt.get("type", "")),
            str(attempt["source"]),
            str(attempt.get("error") or attempt.get("result") or "—"),
        )
    console.print(table)


def library_status_entries(
    items: list[MediaFile], store: FailureStore, root: Path
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in items:
        key = store.key(item.path)
        seen.add(key)
        saved = store.items.get(key)
        active = store.active(item.path)
        if not item.should_convert:
            group, reason = "success", "already HEVC"
        elif active and active.get("outcome") == "success":
            group, reason = "success", str(active.get("result", "successful output exists"))
        elif FailureStore.blocks(active):
            group, reason = "blocked", outcome_reason(active)
        else:
            group = "ready"
            if active and active.get("outcome") == "ready":
                reason = str(active.get("result", "ready"))
            elif saved and saved.get("retry_requested"):
                reason = "retry requested"
            elif saved:
                reason = "source or output changed"
            else:
                reason = "video codec is not HEVC"
        entries.append(
            {
                "group": group,
                "source": str(item.path),
                "reason": reason,
                "size": item.size,
                "duration": item.duration,
            }
        )
    for record in store.records():
        source = Path(str(record["source"]))
        key = store.key(source)
        if key in seen or not path_is_under(source, root):
            continue
        active = store.active(source)
        if (
            active
            and active.get("outcome") == "success"
            and active.get("output")
            and store.key(Path(str(active["output"]))) in seen
        ):
            continue
        if active and active.get("outcome") == "success":
            group, reason = "success", str(active.get("result", "successful output exists"))
        elif FailureStore.blocks(active):
            group, reason = "blocked", outcome_reason(active)
        else:
            group, reason = "stale", "source, output, or settings changed or is missing"
        entries.append(
            {
                "group": group,
                "source": str(source),
                "reason": reason,
                "size": record.get("source_size") or 0,
                "duration": record.get("duration") or 0,
            }
        )
    order = {"ready": 0, "success": 1, "blocked": 2, "stale": 3}
    return sorted(entries, key=lambda item: (order[str(item["group"])], str(item["source"])))


@app.command(name="status")
def library_status(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions."),
    workers: int = typer.Option(2, min=1, max=32, help="Concurrent metadata probes."),
    probe_timeout: float = typer.Option(60, min=1, help="Seconds allowed per metadata probe."),
    cache: Optional[Path] = typer.Option(None, "--cache-file", help="Local probe-cache path."),
    use_cache: bool = typer.Option(True, "--cache/--no-cache", help="Use local probe cache."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """Show ready, successful, blocked, and stale library files."""
    try:
        items = inspect(
            directory, depth, ffprobe, extensions, workers, probe_timeout, cache, use_cache
        )
        entries = library_status_entries(items, FailureStore(), directory)
    except ScanInterrupted as error:
        error_console.print(f"[yellow]{error}[/]")
        raise typer.Exit(130)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    totals = {
        group: {
            "files": sum(entry["group"] == group for entry in entries),
            "bytes": sum(int(entry["size"]) for entry in entries if entry["group"] == group),
            "duration_seconds": sum(
                float(entry["duration"]) for entry in entries if entry["group"] == group
            ),
        }
        for group in ("ready", "success", "blocked", "stale")
    }
    if json_output:
        typer.echo(
            json.dumps({"root": str(directory), "totals": totals, "items": entries}, indent=2)
        )
        return
    labels = {
        "ready": "Ready",
        "success": "Success / not required",
        "blocked": "Blocked",
        "stale": "Missing / stale",
    }
    table = Table(title=f"BrakeSmith status · {directory}", show_lines=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Source", overflow="fold", ratio=3)
    table.add_column("Reason", overflow="fold", ratio=2)
    table.add_column("Size", justify="right")
    table.add_column("Duration", justify="right")
    for entry in entries:
        table.add_row(
            labels[str(entry["group"])],
            str(entry["source"]),
            str(entry["reason"]),
            f"{int(entry['size']) / 1_073_741_824:.2f} GB",
            f"{float(entry['duration']) / 3600:.2f} h",
        )
    console.print(table)
    console.print(" · ".join(f"{labels[group]}: {totals[group]['files']}" for group in totals))


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


@dataclass(frozen=True)
class FormatChoice:
    name: str
    quality: float
    preset: str
    bit_depth: int
    encoder_profile: Optional[str]

    def payload(self) -> dict[str, object]:
        return {
            "format_preset": self.name,
            "quality": self.quality,
            "preset": self.preset,
            "bit_depth": self.bit_depth,
            "encoder_profile": self.encoder_profile,
        }


FORMAT_LABELS = {
    "recommended": "Recommended automatic (Recommended)",
    "highest": "Highest practical quality",
    "high": "High quality",
    "compact": "Compact",
}


def format_description(name: str, choice: FormatChoice) -> str:
    if name == "custom":
        profile = choice.encoder_profile or "automatic profile"
        return (
            f"All files: H.265 {choice.bit_depth}-bit · RF {choice.quality:g} · "
            f"{choice.preset} · {profile} · source audio passthrough · MKV"
        )
    values = FORMAT_PRESETS[name]
    tiers = " · ".join(
        f"{resolution} RF {quality:g}/{preset}" for resolution, (quality, preset) in values.items()
    )
    return (
        f"{tiers} · H.265 Main 10 · MKV · AAC stereo 160k + EAC3 surround "
        "640k (4K 768k) · SRT/ASS passthrough · HDR metadata passthrough"
    )


def choice_from_payload(payload: dict[str, object], fallback: FormatChoice) -> FormatChoice:
    name = str(payload.get("format_preset", fallback.name))
    if name not in {*FORMAT_PRESETS, "custom"}:
        raise BrakeSmithError(f"Unknown saved format preset: {name}")
    return FormatChoice(
        name,
        float(payload.get("quality", fallback.quality)),
        str(payload.get("preset", fallback.preset)),
        int(payload.get("bit_depth", fallback.bit_depth)),
        str(payload["encoder_profile"]) if payload.get("encoder_profile") else None,
    )


def reconcile_format_choice(
    requested: Optional[str],
    quality: float,
    preset: str,
    bit_depth: int,
    encoder_profile: Optional[str],
    non_interactive: bool,
    settings_path: Optional[Path] = None,
) -> FormatChoice:
    fallback = FormatChoice(requested or "recommended", quality, preset, bit_depth, encoder_profile)
    if fallback.name not in {*FORMAT_PRESETS, "custom"}:
        raise BrakeSmithError(f"--format-preset must be {', '.join([*FORMAT_PRESETS, 'custom'])}")
    if non_interactive or requested:
        return fallback

    saved_payload = load_format_settings(settings_path)
    if saved_payload:
        saved = choice_from_payload(saved_payload, fallback)
        action = questionary.select(
            "Format settings",
            choices=[
                Choice(
                    f"Use saved: {FORMAT_LABELS.get(saved.name, 'Custom')}",
                    value="saved",
                    description=format_description(saved.name, saved),
                ),
                Choice(
                    "Change format settings",
                    value="change",
                    description="Review every profile and replace the saved default if wanted.",
                ),
            ],
            default="saved",
            instruction="(↑/↓ move, enter confirm; details follow cursor)",
        ).ask()
        if action is None:
            raise BrakeSmithError("Format selection cancelled")
        if action == "saved":
            return saved

    choices = [
        Choice(
            FORMAT_LABELS[name],
            value=name,
            description=format_description(name, fallback),
        )
        for name in FORMAT_PRESETS
    ]
    choices.append(
        Choice(
            f"Current CLI settings — RF {quality:g}, {preset}",
            value="custom",
            description=format_description("custom", fallback),
        )
    )
    answer = questionary.select(
        "Choose format preset",
        choices=choices,
        default="recommended",
        instruction="(↑/↓ move, enter confirm; details follow cursor)",
    ).ask()
    if answer is None:
        raise BrakeSmithError("Format selection cancelled")
    selected = FormatChoice(answer, quality, preset, bit_depth, encoder_profile)
    if questionary.confirm("Save as the default for future runs?", default=True).ask():
        saved_path = save_format_settings(selected.payload(), settings_path)
        console.print(f"[dim]Saved format settings: {saved_path}[/]")
    return selected


def format_summary(settings: FormatSettings) -> str:
    audio = "AAC 160k + EAC3 640k/768k when surround" if settings.library_audio else "copy"
    return (
        f"{settings.resolution} → x265 {settings.bit_depth}-bit, RF {settings.quality:g}, "
        f"{settings.preset}, {settings.encoder_profile or 'auto profile'}, audio {audio}, MKV"
    )


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


def load_exact_sources(selection: Optional[Path], root: Path) -> list[Path]:
    """Load and validate a stable, ordered source selection."""
    if selection is None:
        return []
    try:
        payload = json.loads(selection.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrakeSmithError(f"Cannot read source selection {selection}: {error}") from error
    if isinstance(payload, dict):
        payload = payload.get("sources")
    if not isinstance(payload, list) or not all(isinstance(value, str) for value in payload):
        raise BrakeSmithError("Source selection must be a JSON list of paths")
    selected: list[Path] = []
    seen: set[str] = set()
    for value in payload:
        source = Path(value).expanduser()
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        if not path_is_under(source, root):
            raise BrakeSmithError(f"Selected source is outside the library: {source}")
        key = os.path.normcase(str(source))
        if key not in seen:
            seen.add(key)
            selected.append(source)
    if not selected:
        raise BrakeSmithError("Source selection is empty")
    return selected


def select_exact_paths(paths: list[Path], root: Path, selection: Optional[Path]) -> list[Path]:
    requested = load_exact_sources(selection, root)
    if not requested:
        return paths
    available = {os.path.normcase(str(path.resolve())): path for path in paths}
    missing = [path for path in requested if os.path.normcase(str(path)) not in available]
    if missing:
        names = ", ".join(str(path) for path in missing[:3])
        suffix = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        raise BrakeSmithError(f"Selected source is not available: {names}{suffix}")
    return [available[os.path.normcase(str(path))] for path in requested]


def select_exact_media(
    items: list[MediaFile], root: Path, selection: Optional[Path]
) -> list[MediaFile]:
    requested = load_exact_sources(selection, root)
    if not requested:
        return items
    available = {os.path.normcase(str(item.path.resolve())): item for item in items}
    missing = [path for path in requested if os.path.normcase(str(path)) not in available]
    if missing:
        names = ", ".join(str(path) for path in missing[:3])
        suffix = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        raise BrakeSmithError(f"Selected source is not an eligible candidate: {names}{suffix}")
    return [available[os.path.normcase(str(path))] for path in requested]


def exclude_remembered_failures(
    items: list[MediaFile],
    store: FailureStore,
    retry_failed: bool,
    policy_hash: str | None = None,
) -> list[MediaFile]:
    blocked = {
        item.path
        for item in items
        if (record := store.active(item.path, policy_hash))
        and (
            record.get("outcome") == "success" or (not retry_failed and FailureStore.blocks(record))
        )
    }
    if blocked:
        console.print(
            f"[yellow]Skipped {len(blocked)} remembered non-candidate(s).[/] "
            "Use `brakesmith failures list` or --retry-blocked."
        )
    return [item for item in items if item.path not in blocked]


def require_matching_existing_output(
    store: FailureStore,
    source: Path,
    destination: Path,
    policy_hash: str | None,
) -> None:
    saved = store.items.get(store.key(source))
    if not saved or saved.get("outcome") != "success":
        return
    active = store.active(source)
    recorded_output = Path(str(saved.get("output", ""))).expanduser().resolve()
    if (
        not active
        or recorded_output != destination.expanduser().resolve()
        or saved.get("policy_hash") != policy_hash
    ):
        raise BrakeSmithError(
            "Existing output belongs to a different recorded source or transcode policy. "
            "Remove it or forget the saved outcome before you adopt it."
        )


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
    selection: Optional[Path] = typer.Option(
        None, exists=True, dir_okay=False, help="JSON list of exact source paths to include."
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
    format_preset: Optional[str] = typer.Option(
        None, help="recommended, highest, high, compact, or custom; default is interactive/saved."
    ),
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
    stop_when_larger: bool = typer.Option(
        False,
        help="Stop replacement encodes when the partial output reaches source size.",
    ),
    include_hevc: bool = typer.Option(False, help="Reprocess files already encoded as HEVC."),
    overrides: Optional[Path] = typer.Option(
        None, exists=True, dir_okay=False, help="Per-source audio_tracks/subtitle_tracks JSON."
    ),
    allow_no_audio: bool = typer.Option(False),
    retry_failed: bool = typer.Option(
        False,
        "--retry-blocked",
        "--retry-failed",
        help="Include remembered non-candidates in this plan.",
    ),
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
        format_preset = prefer_profile(profile_settings, "format_preset", format_preset, None)
        format_preset = str(format_preset) if format_preset else None
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
    if stop_when_larger and not replace_source:
        console.print("[red]Error:[/] --stop-when-larger requires --replace-source.")
        raise typer.Exit(2)
    try:
        format_choice = reconcile_format_choice(
            format_preset,
            quality,
            preset,
            bit_depth,
            encoder_profile,
            non_interactive,
        )
        max_files = reconcile_max_files(max_files, non_interactive)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    overrides_hash = None
    if overrides:
        try:
            overrides_hash = sha256(overrides.read_bytes()).hexdigest()
        except OSError as error:
            console.print(f"[red]Error:[/] Cannot read overrides {overrides}: {error}")
            raise typer.Exit(2)
    policy_hash = policy_digest(
        {
            "format": vars(format_choice),
            "format_values": FORMAT_PRESETS.get(format_choice.name),
            "audio": audio,
            "subtitles": subtitles,
            "unknown_audio": unknown_audio,
            "unknown_subtitles": unknown_subtitles,
            "keep_commentary": keep_commentary,
            "forced_subtitles_only": forced_subtitles_only,
            "exclude_titles": exclude_titles,
            "keep_original": keep_original,
            "original_language": original_language,
            "tune": tune,
            "level": encoder_level,
            "crop": crop,
            "deinterlace": deinterlace,
            "lossless": lossless,
            "replace_source": replace_source,
            "include_hevc": include_hevc,
            "output_directory": output_directory,
            "allow_no_audio": allow_no_audio,
            "overrides": overrides_hash,
        }
    )
    try:
        candidates = [
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
                retry_blocked=retry_failed,
            )
            if item.should_convert or include_hevc
        ]
        failure_store = FailureStore()
        candidates = exclude_remembered_failures(
            candidates, failure_store, retry_failed, policy_hash
        )
        candidates = select_exact_media(candidates, directory, selection)
        items = limit_proposed_files(candidates, max_files)
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
            format_settings = resolve_format_settings(
                item,
                format_choice.name,
                format_choice.quality,
                format_choice.preset,
                format_choice.bit_depth,
                format_choice.encoder_profile,
            )
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
            if format_settings.library_audio:
                selected_subtitles = text_subtitle_tracks(item, selected_subtitles)
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
                    "expected_audio_tracks": expected_audio_track_count(
                        item, selected_audio, format_settings.library_audio
                    ),
                    "replace_source": replace_source,
                    "stop_when_larger": stop_when_larger,
                    "policy_hash": policy_hash,
                    "warnings": fidelity_warnings(item),
                    "format": {
                        "preset": format_settings.name,
                        "resolution": format_settings.resolution,
                        "quality": format_settings.quality,
                        "encoder_preset": format_settings.preset,
                        "bit_depth": format_settings.bit_depth,
                        "encoder_profile": format_settings.encoder_profile,
                    },
                    "command": handbrake_command(
                        executable,
                        item,
                        partial,
                        audio_languages,
                        subtitle_languages,
                        originals[item.path],
                        format_settings.quality,
                        format_settings.preset,
                        extra_audio[item.path],
                        extra_subtitles[item.path],
                        format_settings.bit_depth,
                        tune,
                        format_settings.encoder_profile,
                        encoder_level,
                        crop,
                        deinterlace,
                        lossless,
                        selected_audio,
                        selected_subtitles,
                        format_settings.library_audio,
                        format_settings.library_audio,
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
                "format_preset": format_choice.name,
                "quality": format_choice.quality,
                "preset": format_choice.preset,
                "bit_depth": format_choice.bit_depth,
                "tune": tune,
                "profile": format_choice.encoder_profile,
                "level": encoder_level,
                "crop": crop,
                "deinterlace": deinterlace,
                "lossless": lossless,
                "audio_languages": audio_languages,
                "subtitle_languages": subtitle_languages,
                "replace_source": replace_source,
                "stop_when_larger": stop_when_larger,
                "policy_hash": policy_hash,
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
    retry_failed: bool = typer.Option(
        False,
        "--retry-blocked",
        "--retry-failed",
        help="Process only previously failed or not-smaller items.",
    ),
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
        failure_store = FailureStore()
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
                    if cancellation_requested():
                        raise KeyboardInterrupt
                    source = Path(str(item["source"]))
                    destination = Path(str(item["destination"]))
                    partial = Path(str(item["partial"]))
                    previous = statuses.get(str(source), {})
                    previous_status = previous.get("status") if isinstance(previous, dict) else None
                    if retry_failed and not retryable_plan_status(previous):
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        continue
                    if not retry_failed and previous_status == "completed":
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        continue
                    attempted += 1
                    emit_machine_event(
                        "item",
                        phase="execute",
                        source=str(source),
                        index=file_number,
                        total=len(items),
                    )
                    file_task = progress.add_task(
                        f"[{file_number}/{len(items)}] {source.name}", total=100
                    )
                    diagnostics: deque[str] = deque(maxlen=500)
                    process: Optional[subprocess.Popen[str]] = None
                    failure_type = "source"
                    policy_hash = str(item.get("policy_hash") or "") or None
                    stop_when_larger = bool(item.get("stop_when_larger", False))
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
                        expected_audio = int(
                            item.get("expected_audio_tracks", len(item["audio_tracks"]))
                        )
                        expected_subtitles = len(item["subtitle_tracks"])
                        if not source.exists() and replace_source and destination.exists():
                            failure_type = "validation"
                            require_matching_existing_output(
                                failure_store, source, destination, policy_hash
                            )
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
                            remember_success(
                                failure_store,
                                source,
                                destination,
                                policy_hash,
                                "recovered-published",
                                duration,
                                snapshot.size,
                                snapshot.modified_ns,
                                True,
                            )
                            progress.update(file_task, completed=100)
                            continue
                        ensure_source_unchanged(snapshot)
                        if destination.exists():
                            failure_type = "validation"
                            require_matching_existing_output(
                                failure_store, source, destination, policy_hash
                            )
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
                                failure_type = "source-delete"
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
                                remember_not_smaller(
                                    failure_store,
                                    source,
                                    snapshot.size,
                                    discarded_size,
                                    policy_hash=policy_hash,
                                    duration=duration,
                                )
                            else:
                                remember_success(
                                    failure_store,
                                    source,
                                    destination,
                                    policy_hash,
                                    "validated-existing",
                                    duration,
                                    snapshot.size,
                                    snapshot.modified_ns,
                                    replace_source,
                                )
                        else:
                            if partial.exists():
                                ensure_source_unchanged(snapshot)
                                failure_type = "validation"
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
                                    failure_type = "publish"
                                    partial.replace(destination)
                                    if replace_source:
                                        failure_type = "source-delete"
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
                                    remember_not_smaller(
                                        failure_store,
                                        source,
                                        snapshot.size,
                                        discarded_size,
                                        policy_hash=policy_hash,
                                        duration=duration,
                                    )
                                else:
                                    remember_success(
                                        failure_store,
                                        source,
                                        destination,
                                        policy_hash,
                                        "recovered-partial",
                                        duration,
                                        snapshot.size,
                                        snapshot.modified_ns,
                                        replace_source,
                                    )
                            else:
                                preflight_destination(destination, snapshot.size)
                                command = item["command"]
                                if not isinstance(command, list) or not all(
                                    isinstance(value, str) for value in command
                                ):
                                    raise BrakeSmithError(f"Invalid planned command for {source}")
                                failure_type = "encode"
                                process = subprocess.Popen(
                                    command,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True,
                                    errors="replace",
                                )
                                assert process.stdout is not None
                                early_size: Optional[int] = None
                                for line in process.stdout:
                                    if cancellation_requested():
                                        raise KeyboardInterrupt
                                    diagnostics.append(line)
                                    match = progress_pattern.search(line)
                                    if match:
                                        percent = min(float(match.group(1)), 100)
                                        progress.update(file_task, completed=percent)
                                        progress.update(
                                            total_task,
                                            completed=completed_weight + weight * percent / 100,
                                        )
                                        emit_machine_event(
                                            "progress",
                                            phase="execute",
                                            source=str(source),
                                            index=file_number,
                                            total=len(items),
                                            percent=percent,
                                        )
                                    if replace_source and stop_when_larger:
                                        early_size = partial_reached_source_size(
                                            partial, snapshot.size
                                        )
                                        if early_size is not None:
                                            terminate_process(process)
                                            break
                                if early_size is not None:
                                    cleanup_error = remember_oversized_partial(
                                        failure_store,
                                        source,
                                        partial,
                                        snapshot.size,
                                        early_size,
                                        diagnostics,
                                        policy_hash,
                                        duration,
                                    )
                                    statuses[str(source)] = {
                                        "status": "failed" if cleanup_error else "completed",
                                        "type": "stale-partial" if cleanup_error else "not-smaller",
                                        "output": None,
                                        "result": (
                                            None if cleanup_error else "kept-source-not-smaller"
                                        ),
                                        "early_stop": True,
                                        "source_bytes": snapshot.size,
                                        "output_bytes": early_size,
                                        "cleanup_error": cleanup_error,
                                    }
                                    if cleanup_error:
                                        failures += 1
                                        console.print(f"[red]Failed:[/] {cleanup_error}")
                                    else:
                                        console.print(
                                            f"[yellow]Stopped early:[/] partial reached source size "
                                            f"({early_size} >= {snapshot.size} bytes): {source}"
                                        )
                                    progress.update(file_task, completed=100)
                                    continue
                                if process.wait() != 0:
                                    raise BrakeSmithError(f"HandBrake failed for {source.name}")
                                ensure_source_unchanged(snapshot)
                                failure_type = "validation"
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
                                    failure_type = "publish"
                                    partial.replace(destination)
                                    if replace_source:
                                        failure_type = "source-delete"
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
                                    remember_not_smaller(
                                        failure_store,
                                        source,
                                        snapshot.size,
                                        discarded_size,
                                        diagnostics,
                                        policy_hash,
                                        duration,
                                    )
                                else:
                                    remember_success(
                                        failure_store,
                                        source,
                                        destination,
                                        policy_hash,
                                        "published",
                                        duration,
                                        snapshot.size,
                                        snapshot.modified_ns,
                                        replace_source,
                                    )
                        progress.update(file_task, completed=100)
                    except KeyboardInterrupt:
                        terminate_process(process)
                        cleanup_error = cleanup_partial(partial)
                        statuses[str(source)] = {
                            "status": "cancelled",
                            "error": cleanup_error,
                        }
                        try:
                            failure_store.record(
                                source,
                                "cancelled",
                                "Conversion cancelled",
                                diagnostics,
                                cleanup_error,
                                policy_hash,
                                duration,
                            )
                        except BrakeSmithError:
                            pass
                        write_state(journal_path, state)
                        console.print(
                            "\n[yellow]Cancelled.[/] State saved; completed replacements remain."
                        )
                        raise typer.Exit(130)
                    except Exception as error:  # noqa: BLE001 - durable state needs every failure
                        terminate_process(process)
                        log_path: Optional[Path] = None
                        try:
                            log_path, cleanup_error = remember_failure(
                                failure_store,
                                source,
                                partial,
                                failure_type,
                                str(error),
                                diagnostics,
                                policy_hash,
                                duration,
                            )
                        except BrakeSmithError:
                            cleanup_error = cleanup_partial(partial)
                        failures += 1
                        statuses[str(source)] = {
                            "status": "failed",
                            "type": failure_type,
                            "error": str(error),
                            "cleanup_error": cleanup_error,
                            "log": str(log_path) if log_path else None,
                        }
                        console.print(f"[red]Failed:[/] {source}: {error}")
                    finally:
                        completed_weight += weight
                        progress.update(total_task, completed=completed_weight)
                        progress.remove_task(file_task)
                        write_state(journal_path, state)
                        emit_machine_event(
                            "item-result",
                            phase="execute",
                            source=str(source),
                            index=file_number,
                            total=len(items),
                            result=statuses.get(str(source), {}),
                        )
                    if stop_after_current or (max_failures and failures >= max_failures):
                        break
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        console.print(
            f"[green]State:[/] {journal_path.resolve()} · {attempted} attempted · {failures} failed"
        )
        emit_machine_event(
            "summary",
            phase="execute",
            attempted=attempted,
            failed=failures,
            state_file=str(journal_path.resolve()),
        )
        if failures:
            raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled.[/] State saved; completed work remains.")
        raise typer.Exit(130)
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


def retryable_plan_status(status: object) -> bool:
    return isinstance(status, dict) and (
        status.get("status") in {"failed", "cancelled"}
        or status.get("result") == "kept-source-not-smaller"
    )


def cleanup_partial(path: Path) -> Optional[str]:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return f"Could not remove partial output {path}: {error}"
    return None


def remember_failure(
    store: FailureStore,
    source: Path,
    partial: Path,
    kind: str,
    error: str,
    diagnostics: Iterable[str],
    policy_hash: str | None = None,
    duration: float | None = None,
) -> tuple[Path, Optional[str]]:
    cleanup_error = cleanup_partial(partial)
    record = store.record(
        source,
        kind,
        error,
        diagnostics,
        cleanup_error,
        policy_hash,
        duration,
    )
    return Path(str(record["log"])), cleanup_error


def remember_not_smaller(
    store: FailureStore,
    source: Path,
    source_size: int,
    output_size: int,
    diagnostics: Iterable[str] = (),
    policy_hash: str | None = None,
    duration: float | None = None,
    early: bool = False,
) -> Optional[Path]:
    prefix = "Partial output reached source size" if early else "Output was not smaller"
    message = f"{prefix} ({output_size} >= {source_size} bytes)"
    try:
        record = store.record(
            source,
            "not-smaller",
            message,
            diagnostics,
            policy_hash=policy_hash,
            duration=duration,
        )
    except BrakeSmithError as error:
        console.print(f"[red]Warning:[/] Cannot remember non-candidate: {error}")
        return None
    return Path(str(record["log"]))


def remember_oversized_partial(
    store: FailureStore,
    source: Path,
    partial: Path,
    source_size: int,
    output_size: int,
    diagnostics: Iterable[str],
    policy_hash: str | None,
    duration: float,
) -> Optional[str]:
    cleanup_error = cleanup_partial(partial)
    if cleanup_error:
        try:
            store.record(
                source,
                "stale-partial",
                "Partial output reached source size, but BrakeSmith could not remove it.",
                diagnostics,
                cleanup_error,
                policy_hash,
                duration,
            )
        except BrakeSmithError as error:
            console.print(f"[red]Warning:[/] Cannot remember stale partial: {error}")
        return cleanup_error
    remember_not_smaller(
        store,
        source,
        source_size,
        output_size,
        diagnostics,
        policy_hash,
        duration,
        early=True,
    )
    return None


def remember_success(
    store: FailureStore,
    source: Path,
    output: Path,
    policy_hash: str | None,
    result: str,
    duration: float,
    source_size: int,
    source_modified_ns: int,
    source_deleted: bool,
) -> None:
    try:
        store.succeeded(
            source,
            output,
            policy_hash,
            result,
            duration,
            source_size,
            source_modified_ns,
            source_deleted,
        )
    except BrakeSmithError as error:
        console.print(f"[red]Warning:[/] Cannot save successful outcome: {error}")


def partial_reached_source_size(path: Path, source_size: int) -> Optional[int]:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return size if size >= source_size else None


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


@app.command(name="run")
def run_batch(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    max_files: Optional[int] = typer.Option(
        None, min=1, help="Maximum conversion candidates; interactive runs ask when omitted."
    ),
    selection: Optional[Path] = typer.Option(
        None, exists=True, dir_okay=False, help="JSON list of exact source paths to include."
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
    format_preset: Optional[str] = typer.Option(
        None, help="recommended, highest, high, compact, or custom; default is interactive/saved."
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
    stop_when_larger: bool = typer.Option(
        False,
        help="Stop replacement encodes when the partial output reaches source size.",
    ),
    include_hevc: bool = typer.Option(False, help="Reprocess files already encoded as HEVC."),
    retry_failed: bool = typer.Option(
        False,
        "--retry-blocked",
        "--retry-failed",
        help="Retry remembered non-candidates.",
    ),
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
        format_preset = prefer_profile(profile_settings, "format_preset", format_preset, None)
        format_preset = str(format_preset) if format_preset else None
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
    if stop_when_larger and not replace_source:
        console.print("[red]Error:[/] --stop-when-larger requires --replace-source.")
        raise typer.Exit(2)
    if invalid_existing not in {"fail", "quarantine"}:
        console.print("[red]Error:[/] --invalid-existing must be fail or quarantine")
        raise typer.Exit(2)
    if stale_partial not in {"fail", "quarantine", "delete"}:
        console.print("[red]Error:[/] --stale-partial must be fail, quarantine, or delete")
        raise typer.Exit(2)
    try:
        format_choice = reconcile_format_choice(
            format_preset,
            quality,
            preset,
            bit_depth,
            encoder_profile,
            non_interactive,
        )
        max_files = reconcile_max_files(max_files, non_interactive)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    policy_hash = policy_digest(
        {
            "format": vars(format_choice),
            "format_values": FORMAT_PRESETS.get(format_choice.name),
            "audio": audio,
            "subtitles": subtitles,
            "unknown_audio": unknown_audio,
            "unknown_subtitles": unknown_subtitles,
            "keep_commentary": keep_commentary,
            "forced_subtitles_only": forced_subtitles_only,
            "exclude_titles": exclude_titles,
            "keep_original": keep_original,
            "original_language": original_language,
            "tune": tune,
            "level": encoder_level,
            "crop": crop,
            "deinterlace": deinterlace,
            "lossless": lossless,
            "replace_source": replace_source,
            "output_directory": output_directory,
            "allow_no_audio": allow_no_audio,
        }
    )
    try:
        candidates = [
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
                retry_blocked=retry_failed,
            )
            if item.should_convert or include_hevc
        ]
        failure_store = FailureStore()
        candidates = exclude_remembered_failures(
            candidates, failure_store, retry_failed, policy_hash
        )
        candidates = select_exact_media(candidates, directory, selection)
        items = limit_proposed_files(candidates, max_files)
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
        render(items, directory)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    excluded_titles = [value.strip() for value in exclude_titles.split(",") if value.strip()]
    formats = {
        item.path: resolve_format_settings(
            item,
            format_choice.name,
            format_choice.quality,
            format_choice.preset,
            format_choice.bit_depth,
            format_choice.encoder_profile,
        )
        for item in items
    }
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
        if formats[item.path].library_audio:
            kept_subs = text_subtitle_tracks(item, kept_subs)
        if item.audio and not kept_audio and not allow_no_audio:
            console.print(
                f"[red]Error:[/] No audio selected for {item.path}; choose a language, keep an "
                "unlabelled track, or pass --allow-no-audio."
            )
            raise typer.Exit(2)
        selected_audio[item.path] = kept_audio
        selected_subtitles[item.path] = kept_subs
        console.print(
            f"{item.path.name}: {format_summary(formats[item.path])}; "
            f"audio sources {kept_audio or 'none'}, subtitles {kept_subs or 'none'}, "
            f"original {originals[item.path] or 'unknown'}"
        )
        for warning in fidelity_warnings(item):
            console.print(f"[yellow]Warning:[/] {item.path.name}: {warning}")
    expected_audio_tracks = {
        item.path: expected_audio_track_count(
            item, selected_audio[item.path], formats[item.path].library_audio
        )
        for item in items
    }
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
    if not yes and not Confirm.ask(f"Convert {len(items)} file(s)? {action}", default=True):
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
                emit_machine_event(
                    "item",
                    phase="run",
                    source=str(item.path),
                    index=file_number,
                    total=len(items),
                )
                if destination.exists():
                    try:
                        require_matching_existing_output(
                            failure_store, item.path, destination, policy_hash
                        )
                        validate_output(
                            destination,
                            ffprobe_executable,
                            item.duration,
                            expected_audio_tracks[item.path],
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
                            try:
                                failure_store.record(
                                    item.path,
                                    "existing-output",
                                    str(error),
                                    policy_hash=policy_hash,
                                    duration=item.duration,
                                )
                            except BrakeSmithError as registry_error:
                                console.print(f"[red]Warning:[/] {registry_error}")
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
                                try:
                                    failure_store.record(
                                        item.path,
                                        "source-delete",
                                        str(error),
                                        policy_hash=policy_hash,
                                        duration=item.duration,
                                    )
                                except BrakeSmithError as registry_error:
                                    console.print(f"[red]Warning:[/] {registry_error}")
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
                                    remember_not_smaller(
                                        failure_store,
                                        item.path,
                                        snapshots[item.path].size,
                                        discarded_size,
                                        policy_hash=policy_hash,
                                        duration=item.duration,
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
                                    remember_success(
                                        failure_store,
                                        item.path,
                                        destination,
                                        policy_hash,
                                        "validated-existing",
                                        item.duration,
                                        snapshots[item.path].size,
                                        snapshots[item.path].modified_ns,
                                        True,
                                    )
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
                            remember_success(
                                failure_store,
                                item.path,
                                destination,
                                policy_hash,
                                "validated-existing",
                                item.duration,
                                snapshots[item.path].size,
                                snapshots[item.path].modified_ns,
                                False,
                            )
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
                            expected_audio_tracks[item.path],
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
                        try:
                            failure_store.record(
                                item.path,
                                "stale-partial",
                                message,
                                policy_hash=policy_hash,
                                duration=item.duration,
                            )
                        except BrakeSmithError as registry_error:
                            console.print(f"[red]Warning:[/] {registry_error}")
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
                    formats[item.path].quality,
                    formats[item.path].preset,
                    extra_audio[item.path],
                    extra_subtitles[item.path],
                    formats[item.path].bit_depth,
                    tune,
                    formats[item.path].encoder_profile,
                    encoder_level,
                    crop,
                    deinterlace,
                    lossless,
                    selected_audio[item.path],
                    selected_subtitles[item.path],
                    formats[item.path].library_audio,
                    formats[item.path].library_audio,
                )
                process: Optional[subprocess.Popen[str]] = None
                diagnostics: deque[str] = deque(maxlen=500)
                failure_type = "encode"
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                    )
                    assert process.stdout is not None
                    early_size: Optional[int] = None
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
                            emit_machine_event(
                                "progress",
                                phase="run",
                                source=str(item.path),
                                index=file_number,
                                total=len(items),
                                percent=percent,
                            )
                        if replace_source and stop_when_larger:
                            early_size = partial_reached_source_size(
                                partial, snapshots[item.path].size
                            )
                            if early_size is not None:
                                terminate_process(process)
                                break
                    if early_size is not None:
                        cleanup_error = remember_oversized_partial(
                            failure_store,
                            item.path,
                            partial,
                            snapshots[item.path].size,
                            early_size,
                            diagnostics,
                            policy_hash,
                            item.duration,
                        )
                        if cleanup_error:
                            failures += 1
                            console.print(f"[red]Failed:[/] {cleanup_error}")
                        else:
                            skipped += 1
                            console.print(
                                f"[yellow]Stopped early:[/] partial reached source size "
                                f"({early_size} >= {snapshots[item.path].size} bytes): {item.path}"
                            )
                        summary_entries.append(
                            {
                                "source": str(item.path),
                                "output": None,
                                "status": (
                                    "failed-stale-partial"
                                    if cleanup_error
                                    else "kept-source-not-smaller"
                                ),
                                "early_stop": True,
                                "source_bytes": snapshots[item.path].size,
                                "output_bytes": early_size,
                                "cleanup_error": cleanup_error,
                            }
                        )
                        progress.update(file_task, completed=100)
                        continue
                    if process.wait() != 0:
                        raise BrakeSmithError(f"HandBrake failed for {item.path.name}")
                    ensure_source_unchanged(snapshots[item.path])
                    failure_type = "validation"
                    validate_output(
                        partial,
                        ffprobe_executable,
                        item.duration,
                        expected_audio_tracks[item.path],
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
                        remember_not_smaller(
                            failure_store,
                            item.path,
                            snapshots[item.path].size,
                            discarded_size,
                            diagnostics,
                            policy_hash,
                            item.duration,
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
                    failure_type = "publish"
                    partial.replace(destination)
                    if replace_source:
                        failure_type = "source-delete"
                        delete_replaced_source(item.path, destination)
                    completed += 1
                    remember_success(
                        failure_store,
                        item.path,
                        destination,
                        policy_hash,
                        "published",
                        item.duration,
                        snapshots[item.path].size,
                        snapshots[item.path].modified_ns,
                        replace_source,
                    )
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
                    try:
                        failure_store.record(
                            item.path,
                            "cancelled",
                            "Conversion cancelled",
                            diagnostics,
                            cleanup_error,
                            policy_hash,
                            item.duration,
                        )
                    except BrakeSmithError as registry_error:
                        console.print(f"[red]Warning:[/] {registry_error}")
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
                    failures += 1
                    console.print(f"[red]Failed:[/] {error}")
                    log_path: Optional[Path] = None
                    try:
                        log_path, cleanup_error = remember_failure(
                            failure_store,
                            item.path,
                            partial,
                            failure_type,
                            str(error),
                            diagnostics,
                            policy_hash,
                            item.duration,
                        )
                        console.print(f"[yellow]Failure log:[/] {log_path}")
                    except BrakeSmithError as registry_error:
                        cleanup_error = cleanup_partial(partial)
                        console.print(f"[red]Warning:[/] {registry_error}")
                    summary_entries.append(
                        {
                            "source": str(item.path),
                            "output": str(destination),
                            "status": "failed",
                            "type": failure_type,
                            "error": str(error),
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
    emit_machine_event(
        "summary",
        phase="run",
        completed=completed,
        skipped=skipped,
        failed=failures,
        total=len(items),
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
