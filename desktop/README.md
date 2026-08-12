# BrakeSmith Desktop

BrakeSmith Desktop is an optional Tauri interface for the BrakeSmith CLI. It is not a replacement engine. The Python CLI remains responsible for discovery, plans, validation, cleanup, replacement, and the global outcome registry.

Users can choose either install shape:

- CLI only: install the Python package or standalone executable.
- CLI and interface: install the desktop bundle. The same standalone CLI is included as a sidecar.

HandBrakeCLI and ffprobe remain system requirements. ffmpeg remains optional for full health checks.

## Interface coverage

- Live four-state library overview.
- Searchable media inventory with exact source selection.
- Complete transcode settings and sealed plan review.
- Streaming progress, diagnostic log, safe stop, and plan resume.
- Blocked outcomes, history, retry, forget, stale prune, and log cleanup.
- Quick and full media health checks.
- System, light, and dark themes.
- Custom CLI, HandBrakeCLI, ffprobe, and ffmpeg paths with local toolchain status.
- Project, bug report, author, and Ko-fi links.

There is no account, telemetry, subscription, or payment system.

## Architecture

The React interface calls three Rust commands. Rust starts one `brakesmith bridge --protocol 1` process for each operation. The bridge accepts a strict method allowlist and JSON parameters. Long operations return newline-delimited JSON events. Stop requests use a cooperative marker first, then a bounded process-tree shutdown. Windows uses a kill-on-close Job Object. No shell, daemon, local HTTP service, or arbitrary command execution is used.

The interface resolves BrakeSmith in this order:

1. Custom path from Settings.
2. `BRAKESMITH_CLI`.
3. Bundled sidecar.
4. Development virtual environment.
5. `brakesmith` on `PATH`.

See [UI_SPEC.md](UI_SPEC.md) for the interface contract.

## Develop

Install the platform prerequisites from the [Tauri documentation](https://v2.tauri.app/start/prerequisites/). Then:

```sh
cd desktop
npm install
npm run sidecar
npm run tauri dev
```

`npm run sidecar` uses `BRAKESMITH_SIDECAR_SOURCE`, `../dist/brakesmith`, or the repository virtual-environment entry point. The virtual-environment entry point is for development only.

Browser-only visual preview:

```sh
npm run dev
```

The browser preview is read-only because native CLI access is intentionally unavailable outside Tauri.

## Verify

```sh
npm test
npm run build
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```

## Build an installer

Build the standalone CLI first:

```sh
python -m pip install . pyinstaller
pyinstaller --onefile --name brakesmith src/brakesmith/__main__.py
cd desktop
npm ci
npm run sidecar
npm run tauri build
```

Tauri emits platform-native bundles under `desktop/src-tauri/target/release/bundle`. GitHub Actions builds unsigned Windows, macOS, and Linux artifacts. This project does not create a release from the desktop workflow.
