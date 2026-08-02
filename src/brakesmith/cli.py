from __future__ import annotations

import csv
import io
import json
import re
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .core import (
    BrakeSmithError,
    MediaFile,
    ProbeCache,
    ScanInterrupted,
    atomic_write_json,
    discover,
    ensure_source_unchanged,
    find_executable,
    handbrake_command,
    normalize_languages,
    output_path,
    preflight_destination,
    probe,
    quarantine_file,
    select_tracks,
    snapshot_source,
    validate_destinations,
    validate_output,
)

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
    paths = discover(
        root, limit, extensions.split(",") if extensions else (), errors=traversal_errors
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
        TimeRemainingColumn(),
        console=error_console,
        transient=True,
    )
    executor = ThreadPoolExecutor(
        max_workers=max(1, workers), thread_name_prefix="brakesmith-probe"
    )
    futures = {executor.submit(probe, path, ffprobe, probe_timeout): path for path in pending}
    try:
        with progress:
            task = progress.add_task(
                f"Inspecting {len(paths)} files ({cache_hits} cached)",
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


def render(media: list[MediaFile], root: Path) -> None:
    table = Table(title=f"BrakeSmith scan · {root.resolve()}", show_lines=False)
    table.add_column("Status")
    table.add_column("File", overflow="fold")
    table.add_column("Codec")
    table.add_column("Audio")
    table.add_column("Subtitles")
    table.add_column("Size", justify="right")
    for item in media:
        audio = ", ".join(f"{t.type_index}:{t.language}" for t in item.audio) or "—"
        subs = ", ".join(f"{t.type_index}:{t.language}" for t in item.subtitles) or "—"
        status = "[cyan]convert[/]" if item.should_convert else "[green]HEVC[/]"
        table.add_row(
            status,
            str(item.path.relative_to(root.resolve())),
            item.codec,
            audio,
            subs,
            f"{item.size / 1_073_741_824:.2f} GB",
        )
    console.print(table)
    console.print(
        f"[bold]{len(media)}[/] video(s), [bold cyan]{sum(m.should_convert for m in media)}[/] need conversion"
    )


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
        render(items, directory)


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
            render(items, directory)
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


def reconcile_unknown_tracks(
    items: list[MediaFile], kind: str, policy: str, yes: bool
) -> dict[Path, set[int]]:
    if policy not in {"ask", "keep", "drop"}:
        raise BrakeSmithError(f"--unknown-{kind} must be ask, keep, or drop")
    result: dict[Path, set[int]] = {item.path: set() for item in items}
    for item in items:
        tracks = item.audio if kind == "audio" else item.subtitles
        for track in (candidate for candidate in tracks if candidate.language == "und"):
            if policy == "keep":
                result[item.path].add(track.type_index)
            elif policy == "ask":
                if yes:
                    raise BrakeSmithError(
                        f"Unlabelled {kind} track {track.type_index} in {item.path}; "
                        f"use --unknown-{kind} keep or drop."
                    )
                label = f" ({track.title})" if track.title else ""
                if Confirm.ask(
                    f"Keep unlabelled {kind} track {track.type_index}{label} in "
                    f"[bold]{item.path.name}[/]?",
                    default=False,
                ):
                    result[item.path].add(track.type_index)
    return result


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
    audio: str = typer.Option("fra,eng", help="Audio languages to keep (ISO codes or names)."),
    subtitles: str = typer.Option("fra,eng", help="Subtitle languages to keep."),
    unknown_audio: str = typer.Option("ask", help="Unlabelled audio tracks: ask, keep, or drop."),
    unknown_subtitles: str = typer.Option(
        "ask", help="Unlabelled subtitle tracks: ask, keep, or drop."
    ),
    keep_original: bool = typer.Option(
        True,
        "--keep-original/--no-keep-original",
        help="Also keep detected original-language audio.",
    ),
    original_language: Optional[str] = typer.Option(
        None, help="Fallback original language for files lacking metadata."
    ),
    quality: float = typer.Option(
        18.0, min=0, max=51, help="x265 constant quality; lower is larger/better."
    ),
    preset: str = typer.Option("slow", help="x265 encoder preset."),
    output_directory: Optional[Path] = typer.Option(
        None, help="Mirror results under another directory."
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
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip final confirmation; unresolved originals still fail."
    ),
    handbrake: Optional[Path] = typer.Option(None, help="Path to HandBrakeCLI."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """Review and execute a safe batch conversion."""
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
        items = [
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
        ]
        render(items, directory)
        if not items:
            return
        originals = reconcile_original(items, keep_original, original_language, yes)
        extra_audio = reconcile_unknown_tracks(items, "audio", unknown_audio, yes)
        extra_subtitles = reconcile_unknown_tracks(items, "subtitles", unknown_subtitles, yes)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    audio_languages = normalize_languages(audio.split(","))
    subtitle_languages = normalize_languages(subtitles.split(","))
    selected_audio: dict[Path, list[int]] = {}
    selected_subtitles: dict[Path, list[int]] = {}
    for item in items:
        kept_audio = sorted(
            set(select_tracks(item.audio, audio_languages, originals[item.path]))
            | extra_audio[item.path]
        )
        kept_subs = sorted(
            set(select_tracks(item.subtitles, subtitle_languages)) | extra_subtitles[item.path]
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
    destinations = {
        item.path: output_path(item.path, output_directory, directory) for item in items
    }
    try:
        validate_destinations([(item.path, destinations[item.path]) for item in items])
        snapshots = {item.path: snapshot_source(item.path) for item in items}
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    if not yes and not Confirm.ask(
        f"Convert {len(items)} file(s)? Originals will remain untouched", default=False
    ):
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
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            total_task = progress.add_task("Total", total=len(items))
            for item in items:
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
                            progress.advance(total_task)
                            continue
                    if destination.exists():
                        progress.advance(total_task)
                        continue
                if partial.exists():
                    try:
                        validate_output(
                            partial,
                            ffprobe_executable,
                            item.duration,
                            len(selected_audio[item.path]),
                            len(selected_subtitles[item.path]),
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
                        progress.advance(total_task)
                        continue
                    if stale_partial == "quarantine":
                        quarantined = quarantine_file(partial, f"stale-{classification}")
                        console.print(f"[yellow]Quarantined stale partial:[/] {quarantined}")
                    else:
                        cleanup_error = cleanup_partial(partial)
                        if cleanup_error:
                            raise BrakeSmithError(cleanup_error)
                        console.print(f"[yellow]Deleted stale partial:[/] {partial}")
                file_task = progress.add_task(item.path.name, total=100)
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
                            progress.update(file_task, completed=min(float(match.group(1)), 100))
                    if process.wait() != 0:
                        raise BrakeSmithError(f"HandBrake failed for {item.path.name}")
                    ensure_source_unchanged(snapshots[item.path])
                    validate_output(
                        partial,
                        ffprobe_executable,
                        item.duration,
                        len(selected_audio[item.path]),
                        len(selected_subtitles[item.path]),
                    )
                    partial.replace(destination)
                    completed += 1
                    summary_entries.append(
                        {
                            "source": str(item.path),
                            "output": str(destination),
                            "status": "completed",
                        }
                    )
                    progress.update(file_task, completed=100)
                except KeyboardInterrupt:
                    terminate_process(process)
                    cleanup_error = cleanup_partial(partial)
                    console.print(
                        "\n[yellow]Cancelled.[/] Partial output cleanup attempted; originals untouched."
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
                    progress.advance(total_task)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    console.print(
        f"[green]Done:[/] {completed} completed, {skipped} skipped, {failures} failed. "
        "Originals untouched."
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
