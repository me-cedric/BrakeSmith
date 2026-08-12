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
- Resolution-aware H.265/x265 Main 10 presets for 480p, 720p, 1080p, and 4K.
- AAC stereo plus EAC3 surround library audio; 4K surround gets 768 kbps.
- SRT/ASS subtitle passthrough, chapters, and metadata-friendly MKV output.
- Live discovery, metadata-analysis, per-file, and total progress with timing.
- Ctrl+C cancellation removes only incomplete output.
- Existing output is skipped, making interrupted batches safe to resume.
- Same-directory temporary output works on mounted SMB shares without cross-filesystem moves.
- JSON scan output for automation.
- Complete conversion-candidate exports in JSON, CSV, or plain text.
- Concurrent metadata probing with local change-aware cache and timeouts.
- Persistent non-candidate registry prevents unchanged failed or non-beneficial files from being selected repeatedly.
- Successful-output identity and transcode-policy tracking prevent completed keep-source jobs from being proposed again.
- Four-state library status: ready, success/not required, blocked, and missing/stale.
- Bounded per-file attempt history and centralized diagnostic logs.
- Quick header checks and full frame-decoding health checks.
- Immutable reviewed plans with exact source identity and collision checks.
- Atomic execution journals, restart/resume, failed-only retry, and stop controls.
- Output validation before publication: codec, duration, tracks, chapters, and readability.
- Optional per-file source replacement with codec-aware `x264` → `x265` / `AVC` → `HEVC` names.
- Source identity checks before and after every encode.
- HDR, Dolby Vision, color, interlace, attachment, sidecar, and track-flag inspection.
- Compact/detailed views, grouped summaries, filters, and named TOML profiles.
- Custom extensions for unusual HandBrake-compatible containers.
- Standalone executable artifacts built for macOS, Windows, and Linux.
- Optional Tauri desktop interface with the same CLI engine on macOS, Windows, and Linux.

## Safety model

- Source files remain untouched unless `--replace-source` is explicit.
- Final output appears only after a successful encode.
- Partial output uses `.mkv.part`; cancellation and conversion failure delete it.
- Existing destination files are validated and never silently overwritten.
- Source size, modification time, device, and inode are rechecked before and after encoding.
- Output appears under its final name only after independent ffprobe validation.
- Missing audio, path collisions, insufficient space, edited plans, and changed sources fail safely.
- Batch execution requires review and confirmation unless `--yes` is used.
- `--non-interactive` controls prompts separately from `--yes` confirmation bypass.
- Replace mode publishes and validates the new file before deleting its source. A deletion failure keeps both files and reports failure.
- Replace mode compares exact byte sizes after validation. Equal/larger output is deleted and source is retained.
- Optional `--stop-when-larger` ends a replacement encode when its partial output reaches source size.

## Requirements

- Python 3.9+
- [HandBrakeCLI](https://handbrake.fr/downloads2.php)
- `ffprobe` from [FFmpeg](https://ffmpeg.org/download.html)
- `ffmpeg` for optional full health checks

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

Standalone executables are available as CI artifacts for macOS, Windows, and Linux. They bundle BrakeSmith and Python. HandBrakeCLI and ffprobe remain external requirements. Full health checks also need ffmpeg.

### Optional desktop interface

BrakeSmith Desktop provides the full workflow in a local interface: library state, exact file selection, transcode settings, sealed plan review, progress, safe stop, blocked outcomes, history, health checks, and registry cleanup.

Desktop bundles include the same BrakeSmith CLI as a sidecar. The CLI-only install remains fully supported. HandBrakeCLI and ffprobe stay external requirements. Desktop workflow artifacts are unsigned development builds until normal project releases include them.

See [desktop/README.md](desktop/README.md) for architecture, development, and packaging instructions.

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

Interactive runs first ask for a format preset. `Recommended automatic` is marked and selected by default. Moving the cursor over a preset shows its resolution-specific video, audio, container, subtitle, and HDR details. The selection can be saved; later runs offer `Use saved` to continue immediately or `Change format settings`.

BrakeSmith detects every file independently, so one mixed batch can use 480p, 720p, 1080p, and 4K settings automatically. It then asks how many files to propose, defaulting to one. After scanning, one global picker lists full language names and audio/subtitle counts. Use arrow keys and Space, then press Enter.

Tdarr-style in-place library conversion:

```sh
brakesmith run /smb --replace-source
```

Each successful file becomes MKV/HEVC, unselected audio/subtitle streams are omitted, and codec text in its name is normalized. HandBrake may preserve container attachments such as fonts or cover art. Smaller output replaces its source before the next file starts; equal/larger output is discarded.

For unattended runs, prompts stay disabled and explicit settings apply:

```sh
brakesmith run /smb --replace-source --max-files 5 --format-preset recommended --non-interactive --unknown-audio keep --unknown-subtitles drop --yes
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
| `brakesmith doctor` | Check required tools and optional full-health support. |
| `brakesmith scan [DIRECTORY]` | Inventory all supported videos. Defaults to `.`. |
| `brakesmith scan --json` | Emit machine-readable inventory. |
| `brakesmith status [DIRECTORY]` | Show ready, successful, blocked, and stale library files. |
| `brakesmith history [DIRECTORY]` | Show recent outcomes for each file. |
| `brakesmith retry FILE...` | Release selected blocked files for the next run. |
| `brakesmith health [DIRECTORY] --quick` | Check file headers and stream metadata. |
| `brakesmith health [DIRECTORY] --full` | Decode all video and audio frames. |
| `brakesmith candidates [DIRECTORY]` | Show unblocked videos not already encoded as HEVC. |
| `brakesmith candidates --output candidates.csv` | Save a complete reviewable conversion list. |
| `brakesmith plan --output batch.json` | Create a sealed, non-destructive execution plan. |
| `brakesmith dry-run --output batch.json` | Alias for `plan`. |
| `brakesmith execute batch.json` | Execute or resume a plan using atomic state. |
| `brakesmith run [DIRECTORY]` | Review, reconcile, and convert a batch. |
| `brakesmith failures list` | List remembered failed files and log paths. |
| `brakesmith failures clear` | Clear failure records and centralized logs. |
| `brakesmith failures forget FILE...` | Propose selected files again on later runs. |
| `brakesmith failures prune` | Remove stale records and orphan logs. |
| `brakesmith failures path` | Show failure registry and log directories. |
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
brakesmith execute movie-batch.json --retry-blocked --max-failures 0
```

Plans include a digest and exact source identity. Editing a plan, changing a source, or using mismatched state is refused. State is saved beside the plan after every file.

Add `--replace-source` while creating the plan to seal immediate per-file replacement into it.

Add `--stop-when-larger` to stop wasting encode time after a partial output reaches the source size. The guard uses a known byte count. It does not estimate the final size.

Exit codes: `0` success, `1` batch failure, `2` configuration/preflight failure, `130` cancellation.

## Library state and history

BrakeSmith stores successful, blocked, failed, and cancelled outcomes in `$XDG_STATE_HOME/brakesmith`, or `~/.local/state/brakesmith` when `XDG_STATE_HOME` is unset. Windows uses `%LOCALAPPDATA%\brakesmith`. Media folders receive no state files or logs.

Inspect current library state and recent attempts:

```sh
brakesmith status /Volumes/Media
brakesmith status /Volumes/Media --json
brakesmith history /Volumes/Media
brakesmith history /Volumes/Media --type not-smaller --json
```

Successful records contain source identity, output identity, and a transcode-policy hash. A changed source, changed output, or changed CLI policy becomes eligible for evaluation again. Each source keeps its latest 20 lightweight attempt records. Only the latest diagnostic log is retained for each source.

Metadata probe failures remain blocked to avoid repeated work. A changed ffprobe path or timeout makes them eligible again. Use `retry` or `--retry-blocked` for an immediate retry.

Release selected failures without deleting their history:

```sh
brakesmith retry "/Volumes/Media/movie.mkv"
brakesmith retry --type not-smaller
brakesmith retry --root /Volumes/Media --type encode
```

The next `run` or `plan` can select released files. Override all matching records for one run with:

```sh
brakesmith run /Volumes/Media --retry-blocked
```

`--retry-failed` remains an alias.

`brakesmith candidates` also excludes remembered sources by default. Pass `--include-blocked` to include them in an export or review table.

Inspect or clear failures:

```sh
brakesmith failures list
brakesmith failures list --type not-smaller
brakesmith failures forget "/Volumes/Media/movie.mkv"
brakesmith failures prune
brakesmith failures clear --type not-smaller
brakesmith failures clear --logs-only
brakesmith failures clear --keep-logs
brakesmith failures path
```

Types identify the result or failure stage: `cancelled`, `probe`, `not-smaller`, `source`, `encode`, `validation`, `publish`, `source-delete`, `existing-output`, or `stale-partial`. `forget` deletes selected state. `prune` removes records for missing or changed files plus orphan logs. Clearing failure records does not delete successful history. `--logs-only` preserves skip records; `--keep-logs` clears records but retains diagnostic files until a later prune.

Failed partial outputs are deleted. One safety exception remains: if source deletion fails after a new output was fully validated and published, BrakeSmith keeps both files and records a `source-delete` failure.

## Health checks

Quick mode reads headers and stream metadata with ffprobe:

```sh
brakesmith health /Volumes/Media --quick
```

Full mode decodes all video and audio frames with ffmpeg. It can take as long as playback:

```sh
brakesmith health /Volumes/Media --full
brakesmith health /Volumes/Media --full --timeout 14400 --json
```

Health checks never modify media. Exit code `1` means one or more files failed the check.

## Profiles

Example `~/.config/brakesmith/config.toml`:

```toml
[profiles.archive]
format_preset = "custom"
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

Built-in format presets:

| Profile | 480p | 720p | 1080p | 4K |
| --- | --- | --- | --- | --- |
| Recommended automatic | RF 22, medium | RF 21, medium | RF 20, medium | RF 18, slow |
| Highest practical quality | RF 18, slow | RF 17, slow | RF 16, slow | RF 16, slow |
| High quality | RF 20, slow | RF 19, slow | RF 18, slow | RF 18, slow |
| Compact | RF 25, medium | RF 24, medium | RF 22, medium | RF 20, medium |

All built-in presets use H.265/x265 Main 10 in MKV. Each chosen surround source becomes AAC stereo 160 kbps plus EAC3 5.1 at 640 kbps; 4K puts EAC3 first at 768 kbps. Stereo sources become AAC stereo 160 kbps. SRT/ASS subtitles are kept; bitmap subtitles are omitted. HDR inputs enable dynamic-metadata passthrough.

Custom quality examples:

```sh
brakesmith run . --format-preset custom --quality 16 --preset slower
brakesmith run . --format-preset custom --quality 20 --preset medium
```

Constant-quality encoding is inherently lossy. Lower RF means higher quality and larger output. RF 18–20 is recommended for most 1080p/4K libraries; use `custom` when you need exact encoder values. Custom mode retains the previous source-audio passthrough behavior.

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

The separate desktop workflow tests React and Rust, builds the PyInstaller sidecar, and creates unsigned Tauri bundles on macOS, Windows, and Linux. It uploads workflow artifacts only. It does not create tags or releases.

## License

[MIT](LICENSE)
