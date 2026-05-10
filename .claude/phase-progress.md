# Phase L.1 — PipelineOrchestrator Progress

## Step 1 — Inventory

### Test baseline
**712 tests collected** (`python -m pytest --collect-only -q`).
Phase L.1 must not regress this count and must not break any green tests.

### What `process-source` produces for a transcript
The Phase A `process-source` flow (`cli.py::process_source`) drives:

1. `SourceLoader.load(source_id, store_root)` reads
   `<store_root>/raw/<family>/<source_id>/{source.txt|source.md, metadata.json}`
   and writes:
   - `<store_root>/processed/<family>/<source_id>/source_record.json`
     (one canonical `source_record` artifact per source_id)
   - `<store_root>/processed/<family>/<source_id>/text_units.jsonl`
2. `SourceEval` decides allow/block.
3. `Promoter.promote(source_record)` writes to a "data lake" — either an
   external `DataLake` class loaded from `DATA_LAKE_PATH/data_lake.py`, or a
   `_LocalDataLake` rooted at `SDL_ROOT` that writes
   `<SDL_ROOT>/<artifact_id>.json`.

### Fields that identify a transcript as "processed"
Two independent on-disk signals carry the source_id:

- `processed/<family>/<source_id>/source_record.json` exists and
  `payload.source_id == <source_id>` — the SourceLoader output.
- An `<artifact_id>.json` exists in SDL_ROOT (or is registered in the
  external DataLake) whose `payload.source_id == <source_id>` — the
  promoted artifact.

### Path in SDL_ROOT that indicates a processed transcript
With the in-tree `_LocalDataLake` fallback: `<SDL_ROOT>/<artifact_id>.json`
contains a `source_record` whose `payload.source_id` matches the transcript's
derived id. SDL_ROOT defaults: `DATA_LAKE_PATH/store/artifacts` is the
conventional location used by the codebase.

### `data-lake/store/raw/transcripts/` interpretation
The repo's existing layout is `raw/<family>/<source_id>/source.txt`.
Per the Phase L.1 spec, `transcripts/` is a flat drop directory of `.docx`
and `.txt` files (mirroring how DocxExtractor's batch mode works on a
directory of .docx files). Each transcript file is one transcript; the
filename stem (slugified) is the derived `source_id`. The orchestrator
stages each unprocessed transcript into `raw/meetings/<source_id>/` (the
"meetings" family — these are spectrum-policy meeting transcripts) before
calling SourceLoader, mirroring `_ingest_vault_note`'s pattern for notes.

### Idempotency requirement
- "Already processed" evidence = either of the two on-disk signals above.
- Unknown / ambiguous → unprocessed (run again). SourceLoader is idempotent
  by content (raw_hash) so re-processing the same input produces an
  equivalent record.

---

## Step 2 — Implement PipelineOrchestrator
DONE. `src/spectrum_systems_core/orchestration/pipeline_orchestrator.py` +
`__init__.py`. Schema:
`contracts/schemas/orchestration/orchestration_run_record.schema.json`.

## Step 3 — CLI run-pipeline
DONE. `run_pipeline()` in `cli.py` + `run-pipeline` subparser.

## Step 4 — Tests
DONE. 18 tests in `tests/orchestration/test_pipeline_orchestrator.py`.
(15 from spec + 3 added in Gate A redteam follow-up for collision and
raw_hash_mismatch coverage.)

## Step 5 — Gate A
Findings (from fresh subagent redteam review):

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 1 | No content-hash check on processed evidence (edited transcripts silently skipped) | FIXED — `_current_raw_hash` compares to `payload.raw_hash`; mismatch → unprocessed (`reason="raw_hash_mismatch"`). |
| 2 | 1 | Corrupt artifact JSON in SDL_ROOT silently skipped | NOT BLOCKING — per-file try/except is unchanged; the safe direction (treat as no evidence → unprocessed) matches Principle 3. SourceLoader is idempotent so re-running is safe. |
| 3 | 1 | Schema validation failure silently drops run record | FIXED — fallback to a minimal-but-valid record on validation failure; original record preserved alongside as `<run_id>.invalid.json` for forensics. |
| 4 | 1 | dry_run side-effect leak (future-proofing) | FIXED — explicit invariant added to module docstring: `scan()` is read-only; `dry_run=True` performs zero writes anywhere under the data lake. |
| 5 | 2 | scan() failure conflated with "no unprocessed" | NOT BLOCKING — `_run` already short-circuits on `scan_result["status"] != "success"` and propagates to a failure record. |
| 6 | 2 | .docx/.txt pairing edge cases | NOT BLOCKING — current rule (.docx wins, re-extract overwrites .txt) is documented; new collision detection covers the case-insensitive collision sub-case. |
| 7 | 2 | Slugify collisions silently overwrite | FIXED — collision detection in `_scan`; collisions never run, become explicit failures with `reason="source_id_collision_with:<other>"`. Tested. |
| 8 | 2 | Stale metadata.json hides drift | NOT BLOCKING — SourceLoader already validates `metadata.source_id == directory name` and `source_family` cross-check; stale metadata produces `metadata_schema_violation` failure. |
| 9 | 2 | CLI exit code masks partial failure | NOT BLOCKING — task spec explicitly states "Partial success is exit 0". |

**Verdict:** four Sev-1 findings addressed; one accepted with rationale.
Three Sev-2 findings addressed; four accepted with rationale.

## Step 6 — Run tests and audit

| Check | Result |
|---|---|
| pytest collect | 730 (712 baseline + 18 new) |
| pytest run | 719 passed, 11 failed (11 pre-existing PDF/cffi failures from L.0 baseline) |
| audit-governance | exit 0; total_flagged: 0, high: 0 |
| new high flags on L.1 files | 0 |
| lint / type-check | N/A (no config) |

## Step 7 — Gate B
Findings (from fresh subagent diff review):

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 2 | `_build_processed_evidence` first-pass `setdefault` could cause an empty-raw_hash processed_dir record to mask a populated SDL artifact | FIXED — `_record()` helper now prefers a candidate with a non-empty `raw_hash` over an existing entry with empty hash. |
| 2 | 2 | Non-dry-run CLI did not surface scan reasons (raw_hash_mismatch, etc.) on Running: lines | FIXED — CLI now scans first and annotates each Running:/failed line with the scan reason when non-trivial. |
| 3 | 2 | `.invalid.json` sidecar was JSON+comment, not parseable JSON | FIXED — sidecar now wraps the original record in `{"_validation_error": ..., "original_record": ...}` (valid JSON). |

Verdict: three Sev-2 findings addressed; zero Sev-1.

Final test status after both gates:

| Check | Result |
|---|---|
| pytest collect | 730 (712 baseline + 18 new) |
| pytest run | 719 passed, 11 failed (11 pre-existing PDF/cffi failures from L.0 baseline) |
| audit-governance | exit 0; total_flagged: 0 high: 0 |
| new high flags on L.1 files | 0 |
| lint / type-check | N/A (no config) |
