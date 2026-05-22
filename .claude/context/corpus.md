# Phase context: corpus

## What this phase does

Corpus owns the 13-transcript benchmark set used to drive Phase 4/5
ingestion, opus baselines, and three-way comparisons. The manifest at
`data/corpus/manifest.json` is the single source of truth: schema-validated
load, hash verification, uniqueness and cross-reference checks all run
through the manifest loader. The ingest CLI is the only writer that may
update the manifest, and only as part of a pre-flighted ingestion run.

## Entry points

- `src/spectrum_systems_core/corpus/manifest_loader.py` — schema-validated
  manifest load + hash verification + uniqueness checks.
- `src/spectrum_systems_core/corpus/ingest.py` — `ingest-corpus` CLI
  subcommand: pre-flight gate, `source_record` write, manifest update.
- `src/spectrum_systems_core/corpus/baseline_opus.py` — Phase 4a opus
  baseline run.
- `src/spectrum_systems_core/corpus/status.py` — corpus-mode status
  reporting.

## Artifact types produced or consumed

- Produces: `source_record` (via ingest), `status_report` (via status).
- Consumes: `data/corpus/manifest.json` and per-transcript raw inputs.

## Known blockers

- None known at branch-open time.

## Last PR that touched this phase

- UNKNOWN (no recent numbered PR appears in `git log -- src/spectrum_systems_core/corpus/`).
  Most recent named commit is `9d256df` (merge from main into
  opus-baseline-prompt-cli branch) followed by `16b5003 fix(phase-5):
  backward-compat legacy three-ways`.
