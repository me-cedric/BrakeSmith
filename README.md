<div align="center">
<br />
<h1>BrakeSmith</h1>
<p><strong>Forge clean H.265 libraries with HandBrakeCLI—without risking originals.</strong></p>
<p>
<a href="https://github.com/me-cedric/BrakeSmith/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/me-cedric/BrakeSmith/ci.yml?branch=main&label=CI&logo=github&style=flat" alt="CI Status" /></a>
<a href="LICENSE"><img src="https://img.shields.io/github/license/me-cedric/BrakeSmith?style=flat" alt="MIT License" /></a>
<a href="https://www.python.org"><img src="https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&style=flat" alt="Python 3.9+" /></a>
<a href="https://handbrake.fr"><img src="https://img.shields.io/badge/HandBrakeCLI-required-orange?style=flat" alt="HandBrakeCLI" /></a>
</p>
<br />
</div>

BrakeSmith scans a directory tree, shows every supported video, lets you reconcile uncertain language metadata, then converts selected files to 10-bit H.265 in MKV. French and English audio/subtitles are kept by default; original-language audio is kept when metadata can identify it.

It is review-first. Output is written to a temporary file, atomically renamed only after HandBrake succeeds, and never replaces the source.

## Features

- macOS, Windows, and Linux.
- Recursive scan by default, with exact depth control.
- Exhaustive table of supported videos, codecs, size, audio, and subtitle languages.
- Skips files already using HEVC unless asked to reprocess them.
- Selectable audio and subtitle languages using names or ISO codes.
- Original-language detection from container metadata.
- Interactive reconciliation when original-language metadata is missing.
- Per-track reconciliation for unlabelled audio and subtitles.
- Explicit non-interactive fallback with `--original-language`.
- H.265/x265 10-bit constant-quality encoding; conservative quality 18 and `slow` preset.
- Audio passthrough when MKV supports the source codec, with AAC fallback.
- Subtitle passthrough, chapters, and metadata-friendly MKV output.
- Per-file and total progress with time remaining.
- Ctrl+C cancellation removes only incomplete output.
- Existing output is skipped, making interrupted batches safe to resume.
- Same-directory temporary output works on mounted SMB shares without cross-filesystem moves.
- JSON scan output for automation.
- Complete conversion-candidate exports in JSON, CSV, or plain text.
- Custom extensions for unusual HandBrake-compatible containers.
- Standalone executable artifacts built for macOS, Windows, and Linux.

## Safety model

- Source files are never deleted, moved, renamed, or modified.
- Final output appears only after a successful encode.
- Partial output uses `.brakesmith.mkv.part` and is deleted on cancellation or failure.
- Existing destination files are never overwritten.
- Batch execution requires review and confirmation unless `--yes` is used.
- `--yes --keep-original` refuses ambiguous files unless `--original-language` resolves them.

After inspecting results, remove originals yourself. BrakeSmith deliberately has no destructive replace mode.

## Requirements

- Python 3.9+
- [HandBrakeCLI](https://handbrake.fr/downloads2.php)
- `ffprobe` from [FFmpeg](https://ffmpeg.org/download.html)

macOS with Homebrew:

```sh
brew install handbrake ffmpeg
```

Windows with winget:

```powershell
winget install HandBrake.HandBrake.CLI
winget install Gyan.FFmpeg
```

Ubuntu/Debian:

```sh
sudo apt install handbrake-cli ffmpeg
```

Check readiness:

```sh
brakesmith doctor
```

The HandBrake desktop app does not always include `HandBrakeCLI`. Install the CLI package separately if `doctor` cannot find it.

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/me-cedric/BrakeSmith.git
```

With pipx:

```sh
pipx install git+https://github.com/me-cedric/BrakeSmith.git
```

Standalone executables are available as CI artifacts for macOS, Windows, and Linux. They bundle BrakeSmith and Python; HandBrakeCLI and ffprobe remain external requirements.

From source:

```sh
git clone https://github.com/me-cedric/BrakeSmith.git
cd BrakeSmith
python -m pip install .
```

## Quick start

Scan current directory recursively. This changes nothing:

```sh
brakesmith scan
```

Review and convert:

```sh
brakesmith run
```

Scan another directory:

```sh
brakesmith scan "/path/to/videos"
```

Keep French, English, and detected original audio; keep French and English subtitles:

```sh
brakesmith run "/path/to/videos" --audio fra,eng --subtitles fra,eng --keep-original
```

Unlabelled tracks are reviewed individually by default. For unattended batches, choose an explicit policy:

```sh
brakesmith run "/path/to/videos" --unknown-audio keep --unknown-subtitles drop --yes
```

For unattended processing where missing metadata should be treated as Japanese original audio:

```sh
brakesmith run "/path/to/videos" --audio fra,eng --keep-original --original-language jpn --yes
```

## Commands

| Command | Purpose |
| --- | --- |
| `brakesmith doctor` | Check HandBrakeCLI and ffprobe. |
| `brakesmith scan [DIRECTORY]` | Inventory all supported videos. Defaults to `.`. |
| `brakesmith scan --json` | Emit machine-readable inventory. |
| `brakesmith candidates [DIRECTORY]` | Show only videos not already encoded as HEVC. |
| `brakesmith candidates --output candidates.csv` | Save a complete reviewable conversion list. |
| `brakesmith run [DIRECTORY]` | Review, reconcile, and convert a batch. |
| `brakesmith --version` | Print installed version. |

Depth examples:

```sh
brakesmith scan . --depth 0   # current directory only
brakesmith scan . --depth 1   # current directory and direct children
brakesmith scan . --depth -1  # unlimited recursion; default
```

Add an uncommon container extension without changing source code:

```sh
brakesmith scan . --extensions divx,video
brakesmith run . --extensions divx,video
```

Export every relevant file before planning a large batch:

```sh
brakesmith candidates "/Volumes/Media" --output candidates.csv
brakesmith candidates "/Volumes/Media" --output candidates.json
brakesmith candidates "/Volumes/Media" --output candidates.txt
```

Reports are never overwritten unless `--force` is explicit. This affects only the report, never media.

Quality examples:

```sh
brakesmith run . --quality 16 --preset slower  # larger, slower, higher quality
brakesmith run . --quality 20 --preset medium  # smaller and faster
```

Constant-quality encoding is inherently lossy. Quality 18 is visually transparent for many sources, not mathematically lossless. Audio is copied when possible. Use a lower quality number if preservation matters more than size.

## SMB and network shares

Mount the share through the operating system, then pass its mounted path:

```sh
# macOS
brakesmith run "/Volumes/Media/Movies"

# Linux
brakesmith run "/mnt/media/Movies"

# Windows PowerShell
brakesmith run "Z:\Movies"
```

BrakeSmith treats mounted SMB storage like local storage. By default, temporary and final files stay beside their source, so completion uses a same-share rename. For slow or unreliable networks, encode locally and mirror the directory structure:

```sh
brakesmith run "/Volumes/Media/Movies" --output-directory "$HOME/BrakeSmith Output"
```

Do not disconnect or unmount a share during encoding. If connectivity fails, the original remains untouched and the incomplete `.part` file is removed when possible.

## Language metadata

BrakeSmith uses stream/container language tags reported by ffprobe. ISO-639 codes such as `eng`, `fra`, `jpn`, and common `en`/`fr` aliases work. Media with wrong or absent tags cannot be identified reliably by software.

When `--keep-original` is active and container metadata does not identify the original language, interactive runs ask once per affected file. Non-interactive runs fail safely until you pass `--original-language CODE` or `--no-keep-original`.

Tracks tagged `und` are never silently guessed. Interactive runs ask whether to keep each one. Automated runs must set `--unknown-audio keep|drop` and `--unknown-subtitles keep|drop`; unresolved `ask` policies fail safely with `--yes`.

Inspect and fix incorrect tags with a tool such as MKVToolNix before a large batch.

## Output

`movie.mp4` becomes `movie.brakesmith.mkv`. Existing outputs are skipped. Use `--output-directory` to mirror the input tree elsewhere.

MKV is intentional: it supports more audio and subtitle formats than MP4, reducing conversions and information loss.

## Development

```sh
git clone https://github.com/me-cedric/BrakeSmith.git
cd BrakeSmith
uv sync --extra dev
uv run pytest
uv run ruff check .
uv build
```

CI tests Python 3.9 and 3.13 on macOS, Windows, and Linux, builds wheel/source and standalone distributions, then performs a real HandBrake encode on Linux. A real multilingual encode is also checked on macOS before release.

## License

[MIT](LICENSE)
