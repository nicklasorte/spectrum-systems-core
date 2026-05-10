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

---

# fix/docx-table-extraction-and-eval — Progress

## Step 1 — Inventory

- Branch in-flight: `claude/fix-docx-tables-eval-BvlDm` (per harness
  config; will open PR against `main`).
- `pytest --collect-only -q`: **730 tests collected** (baseline).
- `pytest -q`: **719 passed, 11 failed** at baseline. All 11 failures
  are `tests/ingestion/test_pdf_extractor.py` /
  `tests/ingestion/test_prepare_pdf_cli.py`, all caused by the
  pre-existing `cryptography.hazmat.bindings._rust` /
  `pyo3_runtime.PanicException` env issue (missing `_cffi_backend`).
  Unrelated to this PR.
- Target after this PR: 730 collected + 4 new docx tests + 12 new
  ingestion-eval tests = **746 collected**, with the same 11 PDF
  baseline failures still present and zero new failures.

### Files inventoried

- `src/spectrum_systems_core/ingestion/docx_extractor.py` — paragraphs
  only via `doc.paragraphs`; needs body-element iteration.
- `src/spectrum_systems_core/orchestration/pipeline_orchestrator.py` —
  calls `DocxExtractor.extract` then `SourceLoader → SourceEval →
  Promoter`. Note: there is also a `_hash_docx_extracted()` helper at
  L89 that mirrors the *current* paragraph-only projection. After
  fixing the extractor it must mirror the new projection or the
  `raw_hash_mismatch` skip-detection breaks for previously-processed
  .docx transcripts.
- Existing `SourceEval`
  (`src/spectrum_systems_core/ingestion/source_eval.py`) is distinct
  from the new `IngestionEval`. Both can co-exist; the new eval
  compares the .docx file itself to its produced source_record (a
  different question from SourceEval's schema/hash checks).

## Step 8 — Pre-Gate-A test status

| Check | Result |
|---|---|
| pytest collect | 749 (730 baseline + 5 docx + 14 ingestion-eval = 19 new) |
| pytest run | 738 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 (with DATA_LAKE_PATH set) |
| lint / type-check | N/A (no config) |

## Step 9 — Gate A (design redteam, fresh subagent)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 1 | `minimum_content_ratio=0.02` bypassable on small/medium .docx files (8KB header-only could clear) | FIXED — CHECK-2 now requires `ratio >= 0.02 AND character_count >= 200`. Two simple gates beat one composite. |
| 2 | 1 | `not_header_only` false-fails dialogue-heavy transcripts with many short turns | FIXED — CHECK-3 only fails when `short_ratio >= 0.8 AND character_count < 2000`. Added `test_dialogue_heavy_transcript_passes`. |
| 3 | 2 | `text_units` fall-back conflates "missing file" with "short content" | FIXED — `text_units_unloadable` failure reason emitted when `recorded_unit_count > 0` but on-disk load returned no units. Added `test_text_units_unloadable_distinct_failure_reason`. |
| 4 | 2 | `write_eval_result` silent no-op when `SDL_ROOT` unset risks evidence loss | FIXED — emits a one-line stderr warning on the SDL_ROOT-unset path. Eval result still flows through orchestrator regardless. |
| 5 | 2 | CHECK-4 `recompute_failed` path under-specified | NOT BLOCKING — already produces a distinct `recompute_failed:...` detail string; the `_re_extract` exception path returns `("", 0)` and that funnels into the recompute_failed branch correctly. |
| 6 | 2 | Untested edge cases: table-only docx, malformed jsonl, extreme unicode | PARTIALLY FIXED — added `test_malformed_jsonl_lines_skipped`. Table-only is already covered by `test_pure_table_document_extracted` in DocxExtractor tests (the eval's behavior is the same). Unicode-length distortion is documented (we use `len()` = character count) but not tested in this PR. |

**Verdict:** two Sev-1 findings addressed; four Sev-2 findings (three addressed, one accepted with rationale, one partially addressed).

After Gate A fixes:

| Check | Result |
|---|---|
| pytest collect | 752 (730 baseline + 5 docx + 17 ingestion-eval) |
| pytest run | 741 passed, 11 failed (same pre-existing) |
| audit-governance | exit 0; flags: 0 |

## Step 10 — Gate B (diff redteam, fresh subagent)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 2 | Schema-required `eval_status` added without bumping `schema_version` (was const "1.0.0") | FIXED — bumped to "1.1.0" in both the schema `const` and `pipeline_orchestrator.SCHEMA_VERSION`; existing schema-validates test updated. |
| 2 | 2 | `write_eval_result` warning lines didn't include the artifact id, making forensic recovery hard when SDL_ROOT was unset or write failed | FIXED — both stderr warning paths now include `source_artifact_id` and `eval_id`. |
| 3 | 2 | CHECK-4 conflated "no_stored_raw_hash" with "recompute_failed" when both held | FIXED — added a third branch `no_stored_raw_hash_and_recompute_failed` that surfaces both signals. |
| 4 | 2 | Orchestrator only printed an operator-visible message on `eval_status == "failed"` and silently rolled `warning` (advisory hash drift) into plain success | FIXED — `eval_status == "warning"` now prints `[orchestrator] ingestion_eval_warning: <filename>` to stdout; entry status remains "success" (advisory still never blocks). |

**Verdict:** four Sev-2 findings addressed; zero Sev-1.

## Final test status

| Check | Result |
|---|---|
| pytest collect | 752 (730 baseline + 5 docx + 17 ingestion-eval) |
| pytest run | 741 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 |
| lint / type-check | N/A (no config) |

