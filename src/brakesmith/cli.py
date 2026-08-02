from __future__ import annotations

import csv
import io
import json
import re
import subprocess
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
    discover,
    find_executable,
    handbrake_command,
    normalize_languages,
    output_path,
    probe,
    select_tracks,
)

app = typer.Typer(
    help="Forge a safe, reviewed batch of H.265 files with HandBrakeCLI.", no_args_is_help=True
)
console = Console()


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
    root: Path, depth: int, ffprobe_path: Optional[Path], extensions: str = ""
) -> list[MediaFile]:
    ffprobe = find_executable("ffprobe", ffprobe_path)
    if not ffprobe:
        raise BrakeSmithError("ffprobe not found. Install FFmpeg or pass --ffprobe.")
    limit = None if depth < 0 else depth
    paths = discover(root, limit, extensions.split(",") if extensions else ())
    media = []
    with console.status(f"Inspecting {len(paths)} video files…"):
        for path in paths:
            try:
                media.append(probe(path, ffprobe))
            except BrakeSmithError as error:
                console.print(f"[yellow]Warning:[/] {error}")
    return media


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


@app.command()
def scan(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(
        -1, help="Subdirectory depth; -1 means recursive, 0 means this directory only."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable output."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """List every supported video and whether it should be converted."""
    try:
        items = inspect(directory, depth, ffprobe, extensions)
    except BrakeSmithError as error:
        console.print(f"[red]Error:[/] {error}")
        raise typer.Exit(2)
    if json_output:
        payload = [media_payload(item) for item in items]
        console.print_json(json.dumps(payload))
    else:
        render(items, directory)


@app.command()
def candidates(
    directory: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    depth: int = typer.Option(-1, help="Subdirectory depth; -1 means recursive."),
    output: Optional[Path] = typer.Option(None, help="Write complete .json, .csv, or .txt list."),
    force: bool = typer.Option(False, help="Replace an existing report, never media."),
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """List only files whose video codec is not already HEVC."""
    try:
        items = [
            item for item in inspect(directory, depth, ffprobe, extensions) if item.should_convert
        ]
        if output:
            output = output.expanduser().resolve()
            if output.exists() and not force:
                raise BrakeSmithError(f"Report exists: {output}; pass --force to replace it")
            suffix = output.suffix.lower()
            if suffix == ".json":
                content = json.dumps([media_payload(item) for item in items], indent=2) + "\n"
            elif suffix == ".csv":
                stream = io.StringIO()
                writer = csv.writer(stream)
                writer.writerow(
                    ["path", "codec", "duration_seconds", "size_bytes", "audio", "subtitles"]
                )
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
            console.print(f"[green]Saved:[/] {len(items)} conversion candidate(s) to {output}")
        else:
            render(items, directory)
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
    extensions: str = typer.Option("", help="Extra comma-separated file extensions to inspect."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip final confirmation; unresolved originals still fail."
    ),
    handbrake: Optional[Path] = typer.Option(None, help="Path to HandBrakeCLI."),
    ffprobe: Optional[Path] = typer.Option(None, help="Path to ffprobe."),
) -> None:
    """Review and execute a safe batch conversion."""
    executable = find_executable("HandBrakeCLI", handbrake)
    if not executable:
        console.print("[red]Error:[/] HandBrakeCLI not found. Run `brakesmith doctor`.")
        raise typer.Exit(2)
    try:
        items = [
            m
            for m in inspect(directory, depth, ffprobe, extensions)
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
    for item in items:
        kept_audio = sorted(
            set(select_tracks(item.audio, audio_languages, originals[item.path]))
            | extra_audio[item.path]
        )
        kept_subs = sorted(
            set(select_tracks(item.subtitles, subtitle_languages)) | extra_subtitles[item.path]
        )
        console.print(
            f"{item.path.name}: audio {kept_audio or 'none'}, subtitles {kept_subs or 'none'}, original {originals[item.path] or 'unknown'}"
        )
    if not yes and not Confirm.ask(
        f"Convert {len(items)} file(s)? Originals will remain untouched", default=False
    ):
        console.print("Cancelled. No files changed.")
        return

    progress_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    failures = 0
    completed = 0
    skipped = 0
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        total_task = progress.add_task("Total", total=len(items))
        for item in items:
            destination = output_path(item.path, output_directory, directory)
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_name(destination.name + ".part")
            if destination.exists():
                console.print(f"[yellow]Skip:[/] {destination} exists")
                skipped += 1
                progress.advance(total_task)
                continue
            if partial.exists():
                partial.unlink()
                console.print(f"[yellow]Cleaned stale partial:[/] {partial}")
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
                    match = progress_pattern.search(line)
                    if match:
                        progress.update(file_task, completed=min(float(match.group(1)), 100))
                if process.wait() != 0:
                    raise BrakeSmithError(f"HandBrake failed for {item.path.name}")
                partial.replace(destination)
                completed += 1
                progress.update(file_task, completed=100)
            except KeyboardInterrupt:
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                partial.unlink(missing_ok=True)
                console.print(
                    "\n[yellow]Cancelled.[/] Partial output removed; originals untouched."
                )
                raise typer.Exit(130)
            except BrakeSmithError as error:
                partial.unlink(missing_ok=True)
                failures += 1
                console.print(f"[red]Failed:[/] {error}")
            finally:
                progress.remove_task(file_task)
                progress.advance(total_task)
    console.print(
        f"[green]Done:[/] {completed} completed, {skipped} skipped, {failures} failed. "
        "Originals untouched."
    )
    if failures:
        raise typer.Exit(1)
