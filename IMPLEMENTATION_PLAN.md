# BrakeSmith v0.4 implementation plan

Checked items are implemented, reviewed, and committed. Each stage receives a patch tag. The consolidated milestone receives `v0.4.0`.

## Baseline

- [x] v0.3.0 candidate reports and live SMB cancellation test
- [x] Work from clean writable clone
- [x] Preserve current CLI compatibility where practical

## v0.3.1 — Safety foundations

- [x] Capture source size, modification time, and identity before encoding
- [x] Recheck source immediately before and after encoding
- [x] Refuse output paths colliding across sources
- [x] Preflight output directory permissions and free space
- [x] Require an explicit opt-in for video without audio
- [x] Centralize partial cleanup without masking original errors
- [x] Handle SIGTERM and Ctrl+C consistently
- [x] Add bounded subprocess termination
- [x] Never invoke subprocesses through a shell

## v0.3.2 — Output integrity

- [x] Validate output readability, HEVC codec, duration, selected tracks, and nonzero size
- [x] Quarantine invalid completed output instead of publishing it
- [x] Validate existing outputs before skipping
- [x] Distinguish valid output, invalid output, and stale partial output
- [x] Preserve failed HandBrake diagnostics in a per-file log
- [x] Write final JSON batch summary atomically
- [x] Refuse suspicious duration differences by default

## v0.3.3 — Scalable inventory

- [ ] Add configurable concurrent ffprobe workers with conservative SMB default
- [ ] Add ffprobe timeouts
- [ ] Add local persistent probe cache keyed by path, size, and modification time
- [ ] Never place cache files on scanned shares by default
- [ ] Report scan progress, failures, and cache hits
- [ ] Save partial candidate reports when scanning is interrupted
- [ ] Surface directory traversal and permission errors
- [ ] Add include/exclude globs and size/codec/duration filters
- [ ] Keep machine-readable stdout clean; route status and warnings to stderr

## v0.3.4 — Durable batch plans

- [ ] Add non-destructive `plan`/`--dry-run` workflow
- [ ] Save exact source identity, selections, destination, and encoder settings
- [ ] Reject changed or edited plans unless explicitly regenerated
- [ ] Execute saved plans without rescanning unrelated files
- [ ] Persist completed, skipped, and failed state atomically
- [ ] Resume interrupted batches
- [ ] Retry failed files only
- [ ] Stop after current file
- [ ] Stop after configurable failure count
- [ ] Weight total progress by media duration
- [ ] Estimate source bytes, duration, free-space need, and rough completion scope

## v0.3.5 — Fidelity and track policy

- [ ] Inspect HDR, Dolby Vision, color metadata, interlace, frame rate, and resolution
- [ ] Warn when source features may not survive current HandBrake settings
- [ ] Inspect MKV attachments and external subtitle sidecars
- [ ] Capture track title and default/forced/hearing-impaired/commentary flags
- [ ] Filter tracks by language plus title/flags
- [ ] Improve ISO-639 alias normalization
- [ ] Reconcile unlabelled tracks by assigning a language or keep/drop
- [ ] Support per-file track overrides in saved plans
- [ ] Expose encoder bit depth, tune, profile, level, crop, and deinterlace policy
- [ ] Add optional lossless mode with storage warning
- [ ] Preserve and validate chapters and duration

## v0.3.6 — Large-library UX

- [ ] Add compact, detailed, and machine-readable views
- [ ] Add grouped summaries by codec, resolution, and HDR state
- [ ] Explain candidate/skip reasons
- [ ] Add named TOML profiles and configuration precedence
- [ ] Add one-answer-for-all reconciliation controls
- [ ] Separate `--non-interactive` from confirmation bypass
- [ ] Show complete planned paths and collision status
- [ ] Add shell-friendly exit codes and failure report paths
- [ ] Add paged output only when interactive

## v0.4.0 — Acceptance and release

- [ ] Run lint and focused regression suite
- [ ] Run local multilingual full conversion
- [ ] Run live SMB inventory from cache twice and compare results
- [ ] Back up one non-HEVC SMB source locally and verify checksum
- [ ] Fully convert that SMB source
- [ ] Validate source unchanged and output integrity
- [ ] Remove backup only after verification
- [ ] Update README and migration notes
- [ ] Tag consolidated `v0.4.0` release
- [ ] Push commits and tags to GitHub

## Later milestones

These remain planned after v0.4 because they require deeper media-specific work or operational policy:

- [ ] VMAF/SSIM sampling and visual-quality comparisons
- [ ] Source-size/output-size prediction model
- [ ] Local staging followed by verified SMB copy
- [ ] Bandwidth limiting and scheduling windows
- [ ] Completion notifications and pre/post hooks
- [ ] Multi-title DVD/Blu-ray and multi-angle handling
- [ ] Pause/resume inside a single HandBrake encode
- [ ] Optional controlled source replacement; disabled by default

## Explicitly excluded for now

- Platform/distribution expansion, signing, notarization, package managers, release automation
- Broad testing initiative, fuzzing, coverage targets, and platform test-matrix expansion
