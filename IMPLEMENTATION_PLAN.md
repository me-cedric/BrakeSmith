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

- [x] Add configurable concurrent ffprobe workers with conservative SMB default
- [x] Add ffprobe timeouts
- [x] Add local persistent probe cache keyed by path, size, and modification time
- [x] Never place cache files on scanned shares by default
- [x] Report scan progress, failures, and cache hits
- [x] Save partial candidate reports when scanning is interrupted
- [x] Surface directory traversal and permission errors
- [x] Add include/exclude globs and size/codec/duration filters
- [x] Keep machine-readable stdout clean; route status and warnings to stderr

## v0.3.4 — Durable batch plans

- [x] Add non-destructive `plan`/`--dry-run` workflow
- [x] Save exact source identity, selections, destination, and encoder settings
- [x] Reject changed or edited plans unless explicitly regenerated
- [x] Execute saved plans without rescanning unrelated files
- [x] Persist completed, skipped, and failed state atomically
- [x] Resume interrupted batches
- [x] Retry failed files only
- [x] Stop after current file
- [x] Stop after configurable failure count
- [x] Weight total progress by media duration
- [x] Estimate source bytes, duration, free-space need, and rough completion scope

## v0.3.5 — Fidelity and track policy

- [x] Inspect HDR, Dolby Vision, color metadata, interlace, frame rate, and resolution
- [x] Warn when source features may not survive current HandBrake settings
- [x] Inspect MKV attachments and external subtitle sidecars
- [x] Capture track title and default/forced/hearing-impaired/commentary flags
- [x] Filter tracks by language plus title/flags
- [x] Improve ISO-639 alias normalization
- [x] Reconcile unlabelled tracks by assigning a language or keep/drop
- [x] Support per-file track overrides in saved plans
- [x] Expose encoder bit depth, tune, profile, level, crop, and deinterlace policy
- [x] Add optional lossless mode with storage warning
- [x] Preserve and validate chapters and duration

## v0.3.6 — Large-library UX

- [x] Add compact, detailed, and machine-readable views
- [x] Add grouped summaries by codec, resolution, and HDR state
- [x] Explain candidate/skip reasons
- [x] Add named TOML profiles and configuration precedence
- [x] Add one-answer-for-all reconciliation controls
- [x] Separate `--non-interactive` from confirmation bypass
- [x] Show complete planned paths and collision status
- [x] Add shell-friendly exit codes and failure report paths
- [x] Add paged output only when interactive

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
