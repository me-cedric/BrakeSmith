<div align="center">
<br />
<h1>BrakeSmith</h1>
<p><strong>Forge clean H.265 libraries with HandBrakeCLI, with validated optional source replacement.</strong></p>
<p>
<a href="https://github.com/me-cedric/BrakeSmith/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/me-cedric/BrakeSmith/ci.yml?branch=main&label=CI&logo=github&style=flat" alt="CI Status" /></a>
<a href="LICENSE"><img src="https://img.shields.io/github/license/me-cedric/BrakeSmith?style=flat" alt="MIT License" /></a>
<a href="https://www.python.org"><img src="https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&style=flat" alt="Python 3.9+" /></a>
<a href="https://handbrake.fr"><img src="https://img.shields.io/badge/HandBrakeCLI-required-orange?style=flat" alt="HandBrakeCLI" /></a>
</p>
<br />
</div>

BrakeSmith scans a directory tree, asks once for languages to keep, then converts selected files to 10-bit H.265 in MKV. English and French are preselected by default; all detected languages appear by full name.

It is review-first. Output is written to a temporary file and published only after HandBrake succeeds plus independent ffprobe validation. Sources remain by default; `--replace-source` deletes each source immediately after its validated output is published.

## Features

- macOS, Windows, and Linux.
- Recursive scan by default, with exact depth control.
- Exhaustive table of supported videos, codecs, size, audio, and subtitle languages.
- Skips files already using HEVC unless asked to reprocess them.
- Selectable audio and subtitle languages using names or ISO codes.
- One arrow-key language selection for audio and subtitles, using full names.
- Default-disposition fallback when a file has none of the globally selected languages.
- Original-language detection from container metadata when `--keep-original` is requested.
- One batch selection for unlabelled audio and subtitles.
- Explicit non-interactive fallback with `--original-language`.
- H.265/x265 10-bit constant-quality encoding; conservative quality 18 and `slow` preset.
- Audio passthrough when MKV supports the source codec, with AAC fallback.
- Subtitle passthrough, chapters, and metadata-friendly MKV output.
- Live discovery, metadata-analysis, per-file, and total progress with timing.
- Ctrl+C cancellation removes only incomplete output.
- Existing output is skipped, making interrupted batches safe to resume.
- Same-directory temporary output works on mounted SMB shares without cross-filesystem moves.
- JSON scan output for automation.
- Complete conversion-candidate exports in JSON, CSV, or plain text.
- Concurrent metadata probing with local change-aware cache and timeouts.
- Immutable reviewed plans with exact source identity and collision checks.
- Atomic execution journals, restart/resume, failed-only retry, and stop controls.
- Output validation before publication: codec, duration, tracks, chapters, and readability.
- Optional per-file source replacement with codec-aware `x264` → `x265` / `AVC` → `HEVC` names.
- Source identity checks before and after every encode.
- HDR, Dolby Vision, color, interlace, attachment, sidecar, and track-flag inspection.
- Compact/detailed views, grouped summaries, filters, and named TOML profiles.
- Custom extensions for unusual HandBrake-compatible containers.
- Standalone executable artifacts built for macOS, Windows, and Linux.

## Safety model

- Source files remain untouched unless `--replace-source` is explicit.
- Final output appears only after a successful encode.
- Partial output uses `.mkv.part`; cancellation cleans it and invalid output is quarantined.
- Existing destination files are validated and never silently overwritten.
- Source size, modification time, device, and inode are rechecked before and after encoding.
- Output appears under its final name only after independent ffprobe validation.
- Missing audio, path collisions, insufficient space, edited plans, and changed sources fail safely.
- Batch execution requires review and confirmation unless `--yes` is used.
- `--non-interactive` controls prompts separately from `--yes` confirmation bypass.
- Replace mode publishes and validates the new file before deleting its source. A deletion failure keeps both files and reports failure.
- Replace mode compares exact byte sizes after validation. Equal/larger output is deleted and source is retained.

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

Interactive runs first ask for a quality profile, defaulting to highest practical quality (RF 16, slow), then ask how many files to propose, defaulting to one. After scanning, one global picker lists full language names and audio/subtitle counts. Use arrow keys and Space, then press Enter. English and French start selected; detected extras start unchecked.

Tdarr-style in-place library conversion:

```sh
brakesmith run /smb --replace-source
```

Each successful file becomes MKV/HEVC, unselected streams and image/data attachments are omitted, and codec text in its name is normalized. Smaller output replaces its source before the next file starts; equal/larger output is discarded.

For unattended runs, prompts stay disabled and explicit settings apply:

```sh
brakesmith run /smb --replace-source --max-files 5 --quality 18 --preset slow --non-interactive --unknown-audio keep --unknown-subtitles drop --yes
```

Scan another directory:

```sh
brakesmith scan "/path/to/videos"
```

Keep French, English, and detected original audio; keep French and English subtitles:

```sh
brakesmith run "/path/to/videos" --audio fra,eng --subtitles fra,eng --keep-original
```

Unlabelled tracks appear as one `Undefined` batch choice. If undefined is the only available language, it starts selected. For unattended batches, choose an explicit policy:

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
| `brakesmith plan --output batch.json` | Create a sealed, non-destructive execution plan. |
| `brakesmith dry-run --output batch.json` | Alias for `plan`. |
| `brakesmith execute batch.json` | Execute or resume a plan using atomic state. |
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

Large cached inventory:

```sh
brakesmith candidates "/Volumes/Media" \
  --cache-file "$HOME/.cache/brakesmith/media.json" \
  --workers 2 --probe-timeout 60 \
  --exclude "Extras/*,Samples/*" \
  --output candidates.csv
```

Two workers is a conservative SMB default. The second unchanged scan uses cached metadata.

## Reviewed plans and resume

Create a plan without encoding:

```sh
brakesmith plan "/Volumes/Media/Movies" \
  --output movie-batch.json \
  --output-directory "$HOME/BrakeSmith Output" \
  --audio eng,fra \
  --unknown-audio language:eng \
  --unknown-subtitles drop \
  --no-keep-original \
  --non-interactive
```

Execute or resume it:

```sh
brakesmith execute movie-batch.json
brakesmith execute movie-batch.json --stop-after-current
brakesmith execute movie-batch.json --retry-failed --max-failures 0
```

Plans include a digest and exact source identity. Editing a plan, changing a source, or using mismatched state is refused. State is saved beside the plan after every file.

Add `--replace-source` while creating the plan to seal immediate per-file replacement into it.

Exit codes: `0` success, `1` batch failure, `2` configuration/preflight failure, `130` cancellation.

## Profiles

Example `~/.config/brakesmith/config.toml`:

```toml
[profiles.archive]
audio = "eng,fra"
subtitles = "eng,fra"
unknown_audio = "language:eng"
unknown_subtitles = "drop"
quality = 18
preset = "slow"
bit_depth = 10
workers = 2
probe_timeout = 60
```

Use it with `brakesmith plan ... --profile archive` or `brakesmith run ... --profile archive`. Explicit CLI values win over profile defaults.

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

Do not disconnect or unmount a share during encoding. If connectivity fails, current source remains and incomplete `.part` cleanup is attempted. Sources from earlier completed replace-mode items are already deleted.

## Language metadata

BrakeSmith uses stream/container language tags reported by ffprobe. ISO-639 codes such as `eng`, `fra`, `jpn`, and common `en`/`fr` aliases work. Media with wrong or absent tags cannot be identified reliably by software.

`--keep-original` is opt-in. When active and container metadata does not identify the original language, interactive runs ask once per affected file. Normal runs avoid this by using the batch language picker.

Tracks tagged `und` are never language-guessed. Interactive runs expose one Undefined choice for the whole batch. Automated runs must set `--unknown-audio keep|drop|language:CODE` and `--unknown-subtitles keep|drop|language:CODE`; unresolved `ask` policies fail safely with `--non-interactive`.

If a file contains none of the chosen languages, BrakeSmith keeps its eligible default-disposition tracks. If no audio is marked default, its first eligible audio track is retained. Subtitles without a default flag remain omitted.

Commentary and descriptive tracks are excluded by title by default. Use `--keep-commentary`, `--forced-subtitles-only`, `--exclude-titles`, or per-file plan overrides when needed.

Inspect and fix incorrect tags with a tool such as MKVToolNix before a large batch.

## Output

Default mode: `movie.mp4` becomes `movie.brakesmith.mkv`, source retained.

Replace mode: `Movie.1080p.x264.mp4` becomes `Movie.1080p.x265.mkv`; `Movie.AVC.avi` becomes `Movie.HEVC.mkv`. With no codec token, `.x265` is appended. Validated existing outputs resume safely. Equal/larger outputs are removed while originals remain.

Existing outputs are skipped only after successful validation. Invalid files require an explicit `--invalid-existing quarantine` policy. Stale partial files default to failure and require an explicit policy.

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
