# Migrating to v0.4

v0.4 keeps `scan`, `candidates`, and `run`, while making batch work stricter.

## Behavioral changes

- `--yes` skips final confirmation only. Use `--non-interactive` to forbid prompts.
- Existing output is validated before skip. Invalid output fails by default.
- Stale partial output fails by default; choose `--stale-partial quarantine|delete` explicitly.
- Selecting no audio from a source containing audio fails unless `--allow-no-audio` is explicit.
- Source identity is checked before and after encoding.
- Output duration, HEVC codec, selected track counts, and meaningful chapters are validated.
- Failed completed output is quarantined instead of published or silently deleted.
- Probe cache schema changed; older cache entries are ignored and rebuilt automatically.
- Commentary/description titles are excluded by default unless policy overrides them.

## Recommended workflow

```sh
brakesmith candidates /media --output candidates.csv
brakesmith plan /media --output batch.json
brakesmith execute batch.json
```

`execute` writes `batch.state.json` after every item. Run the same command again to resume.

## Unlabelled tracks

Use `language:CODE` when the language is known:

```sh
--unknown-audio language:eng
--unknown-subtitles language:fra
```

This reconciles selection policy. It does not rewrite incorrect source metadata.
