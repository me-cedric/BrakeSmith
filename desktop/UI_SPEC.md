# BrakeSmith Desktop UI Contract

## Product

BrakeSmith Desktop is an optional interface for BrakeSmith CLI. Users can install the CLI alone or install the desktop bundle. The desktop bundle includes the same CLI binary. HandBrakeCLI and ffprobe stay external requirements. ffmpeg stays optional for full health checks.

The app is for people who manage personal video libraries. It must make safe batch work clear. It must never hide source replacement, failed validation, or a retained partial file.

## Design read

This is a cross-platform desktop product for home-media operators. The visual language is a precision workshop with restrained science-fiction details.

- Design variance: 5. Layouts are ordered but not fully symmetrical.
- Motion intensity: 4. Motion explains state changes and task progress.
- Visual density: 7. Lists are compact. Primary actions have more space.
- Theme: system by default, with light and dark choices.
- Accent: ion lime. It identifies selection, focus, and primary actions.
- Shape rule: 18 px surfaces, 10 px controls, and pill action buttons.

The app uses native CSS. It does not copy an official design system. React supplies component structure. Phosphor supplies one consistent icon family.

## Interface architecture

Three interface options were compared:

1. A generic dispatch command has the smallest API. It also moves too much validation into untyped request data.
2. A broad RPC API gives maximum flexibility. It exposes too many operations for the first desktop release.
3. A job-oriented bridge matches the common workflow and keeps the interface small.

The app uses option 3 with one part from option 2: a versioned protocol.

```text
React
  call_bridge(request, cli_path)
  start_bridge_job(request, cli_path)
  cancel_job(job_id)
      |
Tauri process bridge
      |
brakesmith bridge --protocol 1
      |
BrakeSmith commands and safety rules
```

Each operation starts one process. There is no daemon and no local network server. Requests use JSON. Long jobs use newline-delimited JSON events. Commands never use a shell. The Python CLI owns source validation, output validation, state journals, cleanup, and the outcome registry.

The app resolves the CLI in this order:

1. A valid custom path from Settings.
2. The `BRAKESMITH_CLI` environment variable.
3. The bundled BrakeSmith sidecar.
4. `brakesmith` on `PATH`.

## Navigation

The left rail has six destinations:

- Overview
- Library
- Queue
- Activity
- Outcomes
- Health

Settings is fixed at the bottom. The current library path stays in the top command strip. Users can change the library from every page.

On windows narrower than 960 px, the rail becomes an icon strip. On windows narrower than 720 px, it becomes a bottom navigation bar. Content becomes one column.

## Screens

### Overview

Show ready, complete, blocked, and stale totals. Use one readiness rail instead of four equal cards. Show the current job when one exists. Show the next useful action. Do not show invented savings or time estimates.

### Library

Show one sortable media table. Rows include state, name, codec, resolution, duration, size, and reason. Provide search and state filters. Keep selection across filters. A detail panel shows tracks, warnings, output identity, and recent attempts.

Required states:

- No library selected
- Scan in progress
- Empty library
- Partial scan with warnings
- Ready results
- CLI or ffprobe error

### Queue

Use two columns on wide windows. The left side shows selected files. The right side shows transcode settings. Settings include format preset, quality, encoder preset, bit depth, audio and subtitle languages, unknown-track policy, source replacement, early size stop, and output directory.

The primary action creates a sealed plan. The review state shows destination paths, warnings, source bytes, duration, and file count. Execution starts only after plan review.

### Activity

Show current phase, current item, total progress, elapsed time, and the last useful message. Keep raw output in a collapsible log. Cap visible log history at 500 lines.

Cancel sends a graceful stop request. The screen must say that completed files remain valid and the current partial file will be cleaned. A saved plan can resume later.

### Outcomes

Use tabs for Blocked and History. Blocked rows show type, reason, date, active state, and diagnostic log path. Actions include retry, forget, prune stale records, clear logs, and clear blocked records. Destructive registry actions require confirmation. These actions never delete source media.

### Health

Quick mode is the default. Full mode includes a clear time warning because it decodes all frames. Results use Healthy or Error. Do not use color as the only signal.

### Settings

Show CLI path, detected version, HandBrakeCLI state, ffprobe state, and ffmpeg state. Let the user choose system, light, or dark theme. Store default transcode settings locally.

Provide links to:

- GitHub repository
- Bug reports
- Cedric Meyer's GitHub profile
- Ko-fi: https://ko-fi.com/mecedric

There are no subscription, account, telemetry, or payment controls.

## Visual tokens

Dark theme:

- Canvas: `#0a0c0e`
- Surface: `#111519`
- Raised surface: `#171c21`
- Main text: `#eef2ed`
- Muted text: `#98a19b`
- Accent: `#c7ef68`

Light theme:

- Canvas: `#edf0ec`
- Surface: `#f8faf7`
- Raised surface: `#ffffff`
- Main text: `#171b18`
- Muted text: `#626b64`
- Accent: `#557d00`

Status colors are semantic. Success uses green. Blocked uses amber. Error uses red. Stale uses neutral gray. Accent color never replaces a status color.

Typography uses a system geometric stack for fast offline startup. Numeric data uses the system monospace stack. Headings use tight tracking. Body text stays at 14 px or larger.

## Interaction rules

- All actions have visible hover, active, focus, disabled, and busy states.
- Buttons move only with transform and opacity.
- Page and panel transitions use `cubic-bezier(0.16, 1, 0.3, 1)`.
- Reduced-motion mode removes translation and uses instant opacity changes.
- Loading views use layout-matched skeleton rows.
- Errors stay near the failed control or operation.
- Toasts report short completed actions only.
- Keyboard focus remains visible at all times.
- Primary button labels use three words or fewer.

## Accessibility

- Target WCAG 2.2 AA.
- All icon buttons have accessible names.
- Table selection works with keyboard controls.
- Status includes text and an icon.
- The app does not use placeholder text as a form label.
- Touch targets are at least 40 by 40 px.
- The layout works at 200 percent zoom.
- The theme follows the operating-system preference by default.

## Packaging

Tauri 2 builds installers for Windows, macOS, and Linux. CI builds a PyInstaller BrakeSmith sidecar for each platform, adds the required Tauri target suffix, and then builds the desktop bundle. CLI-only artifacts continue to build separately.

No signing, notarization, tag, or release is part of this change.
