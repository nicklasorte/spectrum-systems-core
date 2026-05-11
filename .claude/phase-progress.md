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

---

# Phase L.2 — MinutesProcessor + GroundTruthLinker

## Step 1 — Inventory

| Item | Result |
|---|---|
| Branch | `claude/phase-l2-ground-truth-MKbJd` (off main) |
| pytest collect (pre-edit) | 752 (baseline) |
| pytest run (pre-edit) | 741 passed, 11 failed (same pre-existing PDF/cffi failures) |
| `DocxExtractor.extract(docx_path, output_path=None) -> dict` | reusable; returns `paragraph_count` (== text-unit count, paragraphs+table-rows), `character_count`, `table_count`, `table_row_count` |
| `SourceLoader.load(source_id, repo_root) -> dict` | produces `source_record` envelope at `processed/<family>/<source_id>/source_record.json` and SDL_ROOT `<artifact_id>.json` |
| SDL_ROOT layout | flat `<artifact_id>.json` files; resolved via `SDL_ROOT` env var or falls back to `<store_root>/artifacts` |
| `store/raw/minutes/` | does NOT yet exist under `data-lake/store/raw/`; MinutesProcessor must handle missing dir gracefully (return `[]`, not error) |
| Existing ingestion schemas dir | `contracts/schemas/ingestion/` (only `source_eval_result.schema.json`) |
| Source-record meeting-date for transcripts | only `payload.metadata.date` (free-form string) — same regex-based normalization will be applied to it on the linker side so transcript and minutes meet on a shared `YYYY-MM-DD` |

## Step 9 — Gate A (design redteam, fresh subagent)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 1 | Reading source_records from BOTH `processed/<family>/<sid>/source_record.json` and `$SDL_ROOT/*.json` could double-count or contradict | FIXED — `_load_transcripts` de-duplicates by `payload.source_id`; `processed/` wins (canonical write). Test `test_dedup_when_processed_and_sdl_root_carry_same_source_id`. |
| 2 | 1 | Pair cardinality undefined when N transcripts share a date with N minutes (N≥2 each) | FIXED — explicit rule: any date with >1 record on either side routes ALL involved to `unmatched_*` with `duplicate_date_collision`. Test `test_duplicate_date_collision_routes_all_to_unmatched`. |
| 3 | 1 | ±1-day window can produce ambiguous medium pairs when a record has multiple ±1-day candidates | FIXED — fuzzy pass routes any record with >1 candidate (or whose sole candidate is itself ambiguous) to unmatched with `ambiguous_fuzzy_match`. Test `test_ambiguous_fuzzy_match_routes_to_unmatched`. |
| 4 | 2 | `linking_report` lacks run-scoping fields | FIXED — added `data_lake_path` to schema and emitted dict. |
| 5 | 2 | `unmatched_*` array element shape under-specified | FIXED — schema requires `source_id`/`minutes_id`, `meeting_date` (nullable), `meeting_name`, `reason` enum. |
| 6 | 2 | `±1 day` boundary undefined | FIXED — explicit `abs((d1 - d2).days) <= 1 and d1 != d2` after `date.fromisoformat`. Test `test_two_day_difference_does_not_match`. |
| 7 | 2 | `status: retired` enum value with no transition rule | NOT BLOCKING — kept in schema for future hand-edits to retire pairs without migration; documented that linker only emits `confirmed`/`pending_review`. |
| 8 | 2 | Filename regex `Jan2026` (month-only) could fabricate a day-of-month | FIXED — month-only-with-year is intentionally NOT matched. Test `test_meeting_date_none_when_not_found` covers `Jan2026`. |

**Verdict:** three Sev-1 findings addressed; six Sev-2 findings (five addressed, one accepted with rationale).

## Step 10 — Pre-Gate-B test status

| Check | Result |
|---|---|
| pytest collect | 782 (752 baseline + 12 minutes + 18 linker = 30 new) |
| pytest run | 771 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 |
| lint / type-check | N/A (no config) |

## Step 11 — Gate B (diff redteam, fresh subagent)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 2 | When transcript T has ≥2 fuzzy candidates (M1, M2) and each Mₙ lists only T as their candidate, T is correctly tagged `ambiguous_fuzzy_match` but M1/M2 fall through to `no_candidate` — wrong reason | FIXED — t-loop ambiguous branch now propagates `ambiguous_fuzzy_match` to every cand-M that is not already in `ambiguous_m_ids`/`paired_m_ids`. `test_ambiguous_fuzzy_match_routes_to_unmatched` extended to assert the same reason on both transcripts and minutes. |

**Verdict:** one Sev-2 finding addressed; zero Sev-1.

## Final test status (Phase L.2)

| Check | Result |
|---|---|
| pytest collect | 782 (752 baseline + 30 new) |
| pytest run | 771 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 |
| lint / type-check | N/A (no config) |


# Confirm Pairs Workflow — Inventory

## Step 1 findings

- Branch: `claude/confirm-pairs-workflow-GlFYx` (assigned by harness; task spec
  said `chore/confirm-pairs-workflow` but harness instructions take precedence).
- Schema: `contracts/schemas/ingestion/ground_truth_pair.schema.json` has
  `status` enum `["confirmed", "pending_review", "retired"]`,
  nullable `confirmed_at` (date-time), nullable `confirmed_by` (string),
  `additionalProperties: false`. **No `content_hash` field — must not add it.**
- Existing workflow `.github/workflows/run-pipeline.yml` already uses the
  pattern: checkout core, checkout `${{ secrets.DATA_LAKE_REPO }}` with
  `${{ secrets.GH_PAT }}` into `data-lake/`, set up Python 3.11, `pip install
  -e ".[dev]"`, then commit-and-push from inside `data-lake/`.
- Data-lake artifact path: `store/artifacts/ground_truth/<pair_id>.json`.
- `jsonschema>=4.0` already in `pyproject.toml` deps.


# fix/ground-truth-linker-date-extraction — Progress

## Step 1 — Inventory

| Item | Result |
|---|---|
| Branch (harness-assigned) | `claude/fix-ground-truth-linker-dates-IS5ZM` |
| pytest collect (pre-edit) | 782 |
| pytest run (pre-edit) | 771 passed, 11 failed (pre-existing PDF/cffi failures unrelated) |
| audit-governance (pre-edit) | exit 0; total_flagged 0; high 0 (DATA_LAKE_PATH set) |

### `source_record` envelope shape (from `contracts/schemas/source_record.schema.json`)
- `payload.title` — string, minLength 1. Set by `SourceLoader` to `metadata["title"]`. The pipeline orchestrator (`pipeline_orchestrator.py:960`) seeds metadata `title` to `txt_path.stem` for raw .txt drops, i.e. the original transcript filename without extension. **This is where the date lives for transcripts staged from the raw .txt drop directory.**
- `payload.raw_path` — string, minLength 1. Slugified path under `raw/<family>/<source_id>/source.txt`; does NOT include the original filename's date.
- `payload.processed_path` — string, minLength 1. `processed/<family>/<source_id>` directory; also does not contain the date.
- `payload.metadata.date` — present only when staged metadata explicitly carried a date (rare in production; the orchestrator currently writes `DEFAULT_DATE` for raw drops).

### Current linker behavior (`ingestion/ground_truth_linker.py:402`)
`_normalize_transcript` reads `payload.metadata.date` only. For real transcripts staged with `DEFAULT_DATE` or no date, this returns None → all transcripts become `no_meeting_date` OR (when `DEFAULT_DATE` is `"1970-01-01"`) every transcript collides on Unix-epoch → `duplicate_date_collision`. This matches the production bug description.

### Current MinutesProcessor date-extraction logic (`ingestion/minutes_processor.py:30-150`)
Module-level regexes:
- `_NUMERIC_DATE_RE` — `M-D-YY[YY]` with separators `[-_./]`. 2-digit years pivoted via `_two_digit_to_full_year` (00-79→20xx, 80-99→19xx).
- `_COMPACT_DATE_RE` — `YYYYMMDD` (8 contiguous digits, not surrounded by digits).
- `_DAY_MONTH_YEAR_RE` — `D[D][-_.\s]?Mon[-_.\s]?YYYY` (4-digit year only — does NOT cover `21Jan26`).
- `_MONTH_DAY_YEAR_RE` — `Month D[D], YYYY` (used on body text only).

Module-level function `extract_meeting_date(filename, text) -> Optional[str]` applies these in priority: filename-COMPACT → filename-DAY_MONTH_YEAR → filename-NUMERIC → text-MONTH_DAY_YEAR → text-DAY_MONTH_YEAR. Already imported by `tests/ingestion/test_minutes_processor.py`.

### Plan
1. Create `spectrum_systems_core/ingestion/date_utils.py` with `extract_meeting_date(text: str) -> Optional[str]` that scans a single string and applies all four regex families in deterministic priority order (COMPACT → DAY_MONTH_YEAR → NUMERIC → MONTH_DAY_YEAR). Move the regex constants and helpers (`_MONTHS`, `_two_digit_to_full_year`, `_safe_iso_date`) into this module.
2. Extend `_DAY_MONTH_YEAR_RE` year part to `(\d{4}|\d{2})` so it also captures `21Jan26`. The lookahead `(?![A-Za-z\d])` already prevents partial matches like `5Feb20` inside `5Feb2026`.
3. In `minutes_processor.py`, keep the existing two-arg `extract_meeting_date(filename, text)` as a thin wrapper: try `extract_meeting_date(Path(filename).stem)` first, then fall back to the first 500 chars of body text. Re-export the symbol so existing test imports keep working with no behavior change.
4. In `ground_truth_linker.py::_normalize_transcript`, build a candidate filename string from `payload.title` → `os.path.basename(payload.raw_path)` → `os.path.basename(payload.processed_path)` (first non-empty wins), strip the file extension, then call `date_utils.extract_meeting_date`. If that returns None, fall back to `payload.metadata.date` (preserves existing test_sdl_root_only_transcripts_also_picked_up etc. fixtures that pass an explicit date). If still None, the transcript is recorded as `no_date_extractable`.
5. Add a new unmatched-reason `no_date_extractable` to the `linking_report.schema.json` enum if it constrains values, and to the linker logic.

### Schema check
`linking_report.schema.json` `reason` enum gains `no_date_extractable` (additive; schema_version stays at 1.0.0).

## Step 5 — Gate A (design redteam, fresh subagent)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | 2 | Body-text scan in MinutesProcessor now applies NUMERIC + COMPACT regexes that were previously filename-only — could silently match section ids / version strings (`1.2.26` → `2026-01-02`) | FIXED — added `date_utils.extract_prose_date(text)` that restricts to MONTH_DAY_YEAR + DAY_MONTH_YEAR only. `MinutesProcessor.extract_meeting_date` calls the prose variant on body text. Filename path still uses the full unified function. |
| 2 | 2 | Latent ambiguity in `NUMERIC_DATE_RE` for European-format filenames (`7/8/26` — Jul 8 vs Aug 7) | NOT BLOCKING — production filenames in `_REAL_TRANSCRIPT_FILENAMES` are uniformly US M-D-Y. Documented preference in `date_utils.py` docstring. Latent risk only. |
| 3 | 2 | `metadata.date == "1970-01-01"` epoch fallback could still cause silent collisions when `payload.title` is generic (e.g. "untitled") and `raw_path`/`processed_path` are empty/missing | FIXED — `_extract_transcript_date` no longer falls back to `metadata.date` at all. Updated test helper `_write_transcript` to inject the `date` argument into the title (mirroring production where the orchestrator stores the original filename in `payload.title`). Updated `test_sdl_root_only_transcripts_also_picked_up` to put the date in the title rather than `metadata.date`. |
| 4 | 2 | `extract_meeting_name` strip behaviour change due to regex sharing | NOT BLOCKING — semantics of `_NUMERIC_DATE_RE` are unchanged; only the alternation ordering inside `(\d{4}\|\d{2})` flipped, which produces the same matches via backtracking. Verified by tracing `2-19-2026` (matches `2026`) and `2-19-26` (matches `26`). |

**Verdict:** zero Sev-1; four Sev-2 findings (two addressed, two accepted with rationale).

## Step 6 — Run tests and audit (post Gate A)

| Check | Result | Exit code |
|---|---|---|
| pytest collect | 795 (782 baseline + 13 new) | — |
| pytest run | 784 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero new regressions) | 1 (pre-existing) |
| audit-governance (`DATA_LAKE_PATH=data-lake`) | total_flagged 0; high 0 | 0 |
| lint / type-check | N/A — no config in repo (per `docs/development/ci.md`) | — |

## Step 7 — Gate B (diff redteam, fresh subagent)

Verdict: **no blocking findings (zero Sev-1, zero Sev-2)**.

The reviewer traced every required case against the diff:

- `2-17-26` → `2026-02-17` ✓ (NUMERIC + 2-digit year pivot)
- `21Jan26` → `2026-01-21` ✓ (DAY_MONTH_YEAR with 2-digit year)
- `5Mar2026` → `2026-03-05` ✓
- All 13 production filenames covered by `test_real_transcript_filenames_all_extract_correctly` (date_utils) and `test_all_13_pairs_match_with_real_filenames` (linker integration).
- `MinutesProcessor` body-text scan deliberately tightened to prose-only (`extract_prose_date`); not a regression — confirmed `1.2.26` and `20251201` cannot false-match in body text.
- `metadata.date` fallback removed; `_extract_transcript_date` only reads filename candidates.
- `None == None` short-circuit confirmed in `_link` (None-dated records routed to unmatched before bucketing).
- `_filename_candidates` ordering: title → `raw_path` basename → `processed_path` basename.
- `extract_meeting_name` regex semantics unchanged (alternation order flip in `(\d{4}|\d{2})` produces same matches via greedy/backtracking).

One Sev-3 docstring inaccuracy noted by the reviewer (the `_extract_transcript_date` docstring's "2026.01.22" example) — not reported per rubric, no action taken.


# fix/duplicate-minutes-and-directory-filtering — Progress

## Step 1 — Inventory

| Item | Result |
|---|---|
| Branch (harness-assigned) | `claude/fix-duplicate-minutes-Gu6XK` |
| pytest collect (pre-edit) | 795 |
| Workflow updated | `.github/workflows/run-pipeline.yml` (already wires `link-ground-truth --process-minutes`) |
| `data-lake/store/raw/minutes/` in repo | absent (production-only path; tests use tmp_path) |
| `data-lake/store/raw/transcripts/` in repo | absent (production-only path) |
| Existing minutes_record artifacts in this checkout | none |

### Schema constraint surfaced as a stop condition
`contracts/schemas/ingestion/minutes_record.schema.json` has
`additionalProperties: false` and **no `status` field**. The task spec
asks for `status: "retired"` + `retired_reason: "duplicate"` on retired
duplicates, which would require a schema bump.

User selected **Move to retired/ subdir**: duplicate `minutes_record`
JSONs are moved to `<sdl_root>/minutes/retired/<id>.json` with a sidecar
`<sdl_root>/minutes/retired/<id>.retired_reason.json` for audit. The
existing linker (`_load_minutes`) uses `minutes_dir.glob("*.json")`
(non-recursive) so files under `retired/` are excluded automatically. No
schema change required for `minutes_record`.

### Schema change for Fix B
`orchestration_run_record` (current `1.1.0`) gains a top-level
`filtered_from_transcripts` array; bumped to `1.2.0`. The `results`
array gains a new `status` enum value `"filtered"` so per-file rows can
be emitted alongside the aggregate field.

## Step 2 — Plan
1. Fix A: `_process()` extracts the .docx, computes `raw_hash` (existing
   formula = `sha256(extracted_text.utf-8)`), then scans
   `<SDL_ROOT>/minutes/*.json` (non-recursive) for an existing record
   with the same `raw_hash`. Match → return `status="skipped"` with
   `skipped_reason="already_processed"`. No artifact write.
2. Fix B: `_scan()` filters `*.docx`/`*.txt` files in
   `store/raw/transcripts/` whose name contains `"minutes"`
   (case-insensitive) into a new `filtered_from_transcripts` list;
   filtered files NEVER reach the runner and are NEVER counted as
   failures. Print each filtered file with the warning line. `_run()`
   includes the list in the orchestration_run_record.
3. Fix C: new `--deduplicate` flag on `link-ground-truth` runs a
   raw_hash-based dedup *before* linking. Per group of 2+: keep the
   oldest by `created_at` and move the rest to
   `<sdl_root>/minutes/retired/`. Schema-validate the kept record
   before treating it as authoritative.


## Step 3-6 — Implementation complete

| File | Change |
|---|---|
| `src/spectrum_systems_core/ingestion/minutes_processor.py` | `_process()` now does an idempotency check via `_find_existing_minutes_by_hash` before writing. New `_skipped()` result helper. `process_directory()` docstring documents skipped status. |
| `src/spectrum_systems_core/ingestion/minutes_deduplicator.py` | NEW. `deduplicate_minutes(data_lake_path) -> dict`. Groups by `raw_hash`, validates keeper, moves dups to `retired/` with sidecar. Never deletes; never raises. |
| `src/spectrum_systems_core/ingestion/__init__.py` | re-exports `deduplicate_minutes`. |
| `src/spectrum_systems_core/orchestration/pipeline_orchestrator.py` | `_scan()` filters `.docx`/`.txt` files containing "minutes" → new `filtered_from_transcripts` array; prints per-file warning. `_run()` threads the list into the orchestration record. SCHEMA_VERSION bumped 1.1.0 → 1.2.0. |
| `contracts/schemas/orchestration/orchestration_run_record.schema.json` | `schema_version` const → "1.2.0". New top-level required `filtered_from_transcripts` array. New `results.items.status` enum value `"filtered"`. |
| `src/spectrum_systems_core/cli.py` | new `--deduplicate` flag on `link-ground-truth`; runs dedup BEFORE linking; surfaces `(failures: N, skipped: N)` from minutes pass. |
| `.github/workflows/run-pipeline.yml` | `link-ground-truth` step now passes `--deduplicate`. YAML validated. |

## Step 7 — Pre-Gate-A test status

| Check | Result |
|---|---|
| pytest collect | 809 (795 baseline + 14 new) |
| pytest run | 798 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 (with DATA_LAKE_PATH set) |
| lint / type-check | N/A (no config) |

## Step 7 — Gate A (design redteam, fresh subagent)

**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

Reviewer walked through every Sev-1/2 hazard called out in the rubric plus several adjacent ones; none rose above Sev-3. Spot-checks resolved every explicit question:

- Hash formula consistency: same `sha256:` prefix on both write and idempotency-scan sides.
- Idempotency scan is non-recursive, so `retired/` is excluded from glob.
- Hash collision: SHA-256 collision is computationally infeasible; same-hash treated as same-content is the only sane choice.
- Filter applies to `.docx` and `.txt` (extracted minutes siblings filtered too).
- Filter never adds to `failed_this_run`; aggregated as top-level `filtered_from_transcripts` AND per-file rows with `status: "filtered"` in `results`.
- Failed processing run does not write any artifact, so a re-run naturally retries.
- Schema bump 1.1.0 → 1.2.0 is forward-write only; no reader re-validates old records.


---

# Phase L.3 — Same-day collision resolution via family token matching

## Step 1 — Inventory

### Test baseline
**809 tests collected** (`python -m pytest --collect-only -q`).

### Root cause confirmed

Read `src/spectrum_systems_core/ingestion/ground_truth_linker.py` (623 lines).

**Root cause A confirmed**: `_load_transcripts()` (lines 335-383) does NOT
filter on filename keywords. It loads source_records from
`processed/meetings/<sid>/source_record.json` and from flat
`$SDL_ROOT/<artifact_id>.json` files. The SDL_ROOT loop only checks
`payload.source_family == "meetings"`. PipelineOrchestrator's "minutes"
filter applies only at scan time for files in `raw/transcripts/` —
artifacts already promoted to SDL_ROOT before that filter shipped remain
in the lake and are still pulled in as transcript candidates.

**Root cause B confirmed**: `_link()` (lines 133-149) routes ALL records
on a date with N>1 or M>1 to `duplicate_date_collision`. No
disambiguation — the linker refuses to pick.

### Files to change
- `src/spectrum_systems_core/ingestion/date_utils.py` — add `family_tokens()`.
- `src/spectrum_systems_core/ingestion/ground_truth_linker.py` — Fix A
  (filter "minutes" titles from transcript candidate pool) + Fix B
  (greedy family-token matching when N>1 or M>1 on a date).
- `tests/ingestion/test_ground_truth_linker.py` — update existing
  `test_all_13_pairs_match_with_real_filenames` to expect 13 pairs;
  add new tests for filter, family_tokens, greedy match, end-to-end
  with bad-record fixtures.


## Step 5 — Gate A (design redteam, fresh subagent)

**Verdict: 1 Sev-1 found and fixed in iteration 1, then re-verified clean.**

Iteration 1 finding: `pairs_medium` was referenced inside the date-bucket
loop at the new collision-resolution branch but the existing code only
initialized it AFTER the loop (line 193). Any same-day collision that
exercised the medium-confidence sole-leftover branch would `UnboundLocalError`
and the outer `link()` would swallow it as a generic failure, dropping
EVERY pair from the run. Fix: initialise `pairs_medium` alongside
`pairs_high` before the loop. Added regression test
`test_same_day_collision_medium_pair_when_sole_leftover`.

Iteration 2 verdict: no blocking findings.

## Step 6 — Test status post-fix

| Check | Result |
|---|---|
| pytest collect | 819 (809 baseline + 10 new) |
| pytest run | 808 passed, 11 failed (same 11 pre-existing PDF failures) |
| audit-governance | exit 0; total_flagged: 0, high: 0 |
| family_tokens fixture-spec verification | ✓ all 10 spot-checks pass |

## Step 7 — Gate B (diff redteam, fresh subagent)

**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

Reviewer walked the four required spot-checks:
1. Tie-break is deterministic — sort key `(-overlap, source_artifact_id, minutes_artifact_id)`.
2. ``adjudication`` is NOT in the stopword set.
3. Filter checks both ``title`` AND ``raw_path``.
4. ``test_all_13_pairs_match_end_to_end`` exists and asserts exactly 13 pairs.

Plus walked through ``family_tokens``, the greedy collision resolver, the
``pairs_medium`` initialization fix, and confirmed no valid transcript is
removed by the substring filter under the production fixture set.

# Cleanup duplicate ground_truth_pair artifacts

## Step 1 — Inventory

- `scripts/confirm_pairs.py` is the reference. It scans
  `$SDL_ROOT/store/artifacts/ground_truth/*.json` (top level only),
  loads each artifact as JSON, mutates `status`/`confirmed_at`/`confirmed_by`,
  validates against `contracts/schemas/ingestion/ground_truth_pair.schema.json`,
  and writes back with `json.dump(..., indent=2, sort_keys=True)` plus
  trailing newline. CLI args: `--data-lake`, `--schema-dir`, `--dry-run`.
- `.github/workflows/confirm-pairs.yml` is the workflow template — checkout
  spectrum-systems-core, checkout `${{ secrets.DATA_LAKE_REPO }}` into
  `data-lake/` with `${{ secrets.GH_PAT }}`, set up Python 3.11, install
  via `pip install -e ".[dev]"`, run dry-run unconditionally, then run the
  real action conditionally on the input flag, then commit/push from the
  `data-lake/` checkout as `spectrum-pipeline[bot]`.
- Pair schema enums: `status ∈ {confirmed, pending_review, retired}`.
  `retired` is valid for an in-place status update too, but the task spec
  asks to MOVE the file to `ground_truth/retired/<pair_id>.json` and
  preserve the original artifact unchanged. Sidecar
  `<pair_id>.retired_reason.json` carries `original_pair_id`,
  `retired_at`, `retired_reason="duplicate_of:<kept_pair_id>"`,
  `kept_pair_id`.

## Step 2 — Plan

Cleanup script mirrors `confirm_pairs.py` structure:
1. Scan `$SDL_ROOT/store/artifacts/ground_truth/*.json` top level only
   (subdirectories like `retired/` are skipped because `iterdir()` returns
   the dir entry with `suffix == ""`).
2. Load JSON, skip files that aren't ground_truth_pair artifacts
   (no `pair_id` / wrong `provenance.produced_by`).
3. Group by `(source_artifact_id, minutes_artifact_id)`. Singletons: no-op.
4. Per duplicate group: sort by `created_at` ascending; pick the oldest
   `confirmed` artifact as keep, falling back to the oldest overall.
   Validate the kept artifact against schema — on failure, skip group.
5. Move every non-kept artifact to `retired/<pair_id>.json` (`shutil.move`)
   and write the sidecar JSON next to it.
6. Print a row per kept group plus a summary line.
7. `--dry-run` short-circuits the move/sidecar write.
8. Wrap each group in `try/except` so one bad group doesn't abort the
   rest. Never raise.

Workflow file mirrors `confirm-pairs.yml`: same checkout/setup steps, same
commit/push tail. Input is `dry_run` (default `'true'`), choice of
`'true'`/`'false'`. Dry-run preview step always runs; real run + commit
step gated by `dry_run == 'false'`.

---

# Phase L.3 — Full Pipeline Orchestration + --force Flag

Branch: `claude/full-pipeline-orchestration-lOr62` (harness-assigned;
task spec said `phase-L3/full-pipeline-orchestration`).

## Step 1 — Inventory

### Test baseline
**828 tests collected** (`python -m pytest --collect-only -q`). Phase L.3
must not regress this count and must not break any green tests.

### CLI commands available (from `cli.py::_build_parser`)
`process-source`, `prepare-pdf`, `extract-stories`, `promote-knowledge`,
`extract-claims`, `process-comments`, `approve-revisions`, `format-paper`,
`certify-paper`, `build-agency-profile`, `predict-objections`,
`track-outcome`, `synthesize`, `record-run`, `record-outcome`,
`compare-runs`, `record-override`, `promote-eval-case`, `audit-entropy`,
`ask-memory`, `audit-governance`, `extract-docx`, `run-pipeline`,
`link-ground-truth`, `apply-compression`.

### Pipeline stage map — actual CLI signatures vs. task spec

| Stage | Task spec | Actual CLI command | Underlying module | Programmatically runnable? |
|---|---|---|---|---|
| 1 | `process-source` | `process-source --source-id X` (Phase A) | `SourceLoader` + `SourceEval` + `Promoter` (already invoked by orchestrator's `_default_runner`) | YES — already wired |
| 2 | `extract-stories --source-id X` | matches | `Chunker` + `StoryExtractor` + `StoryEval` + `StoryworthyFilter` | YES |
| 3 | `promote-knowledge --source-id X` | **MISMATCH**: actual CLI requires `--artifact-id X --source-id Y --artifact-type {concept\|theme\|analogy\|connection}` and is a *human-gated single-artifact promotion* (per FINDING-C-003 — no auto-promotion path) | `KnowledgeSynthesizer.synthesize_{concepts,themes,analogies}` is the closest automated step; it reads `stories/promoted/` (human-gated input) and writes `knowledge/<type>.jsonl` candidates | YES — but with the adjustment below |
| 4 | `extract-claims --source-id X` | matches | `ClaimExtractor` + `AssumptionExtractor` + `ClaimEval` + `EvidenceBuilder` + `ContradictionDetector` + `EvidenceEval` (reads `text_units.jsonl` directly — does NOT depend on Stage 2 output) | YES |
| 5 | `synthesize --audience X --purpose Y` | matches | `BundleAssembler` + `BundleEval` + `ThemeSynthesizer` + `StoryMatrix` + `ReportGenerator` + `GroundingEval` + `KeynoteGenerator` + `KeynoteEval` — global, one run | YES |

### Stage-3 adjustment (per Stop Conditions)
The `promote-knowledge` CLI is human-gated and cannot run in an automated
orchestrator. The closest automated step is `KnowledgeSynthesizer`, which
produces **candidate** knowledge artifacts (concepts/themes/analogies)
from promoted stories. In a fresh run with no human promotion yet, this
is a no-op that succeeds with 0 records — which is the correct behavior:
the automated pipeline produces what it can, and humans promote
individual artifacts via the existing CLI.

**Decision:** Stage 3 in the orchestrator wraps
`KnowledgeSynthesizer.synthesize_{concepts,themes,analogies}`. The
existing CLI `promote-knowledge` command is unchanged — the orchestrator
does not call it. The stage is logged as `extract-knowledge` internally
but maps to the `promote_knowledge` slot in the pipeline_stages dict so
the task spec's vocabulary is preserved.

### "Already processed" markers per stage
Idempotency checks (orchestrator skips when `force=False` and):

| Stage | Marker file |
|---|---|
| 1 (process-source) | existing `source_record.json` for this `source_id` under `processed/<family>/<sid>/` or matching SDL artifact (existing behavior) |
| 2 (extract-stories) | `processed/<family>/<sid>/stories/candidates.jsonl` exists |
| 3 (promote-knowledge / knowledge-extract) | any of `processed/<family>/<sid>/knowledge/{concepts,themes,analogies}.jsonl` exists |
| 4 (extract-claims) | `processed/<family>/<sid>/paper/claims.jsonl` exists |
| 5 (synthesize) | runs once globally per orchestrator invocation; skipped if no transcript reached Stage 4 success AND not forced |

### Stage dependency (per task spec)
- If Stage 2 fails for a transcript → skip Stages 3 and 4 for THAT
  transcript only. Other transcripts continue independently.
- Stage 5 (synthesize) runs once after all transcripts. It runs iff at
  least one transcript reached Stage 4 success this run, OR `force=True`
  and at least one transcript reached Stage 4 success this run. Skipped
  if all transcripts skipped, all failed before Stage 4, or no
  transcripts present.

### Force flag semantics
- `scan(force=True)`: reports all transcripts as "to run" regardless of
  existing source_record evidence.
- `run(force=True)`: re-runs every stage per transcript regardless of
  existing artifacts. The orchestrator NEVER issues `rm` operations.
  Underlying modules (StoryExtractor, etc.) may overwrite their own
  working-state files (`candidates.jsonl`, `claims.jsonl`) — this is
  pre-existing module behavior, not new in this phase. SDL-promoted
  artifacts (e.g., `source_record`) are content-addressed by their
  Promoter; re-running with unchanged content produces the same
  artifact_id (idempotent at the SDL layer), while changed content
  produces a new artifact with a new id. Old SDL files are never
  removed.

### Schema changes (`orchestration_run_record.schema.json`, bump 1.2.0 → 1.3.0)
Additive top-level fields: `force` (bool), `synthesize_status` (enum),
`total_stages_completed` (int), `total_stages_failed` (int). Per-result
adds `pipeline_stages` object with one entry per stage. Schema bumped to
1.3.0; readers of 1.2.0 records will not re-validate older files.

### Files to change
- `src/spectrum_systems_core/orchestration/pipeline_orchestrator.py`
  — add `force` param, Stage 2-5 chain, per-stage idempotency checks.
- `contracts/schemas/orchestration/orchestration_run_record.schema.json`
  — schema_version bump + new fields.
- `src/spectrum_systems_core/cli.py::run_pipeline` — add `--force` flag,
  print per-stage status.
- `.github/workflows/run-pipeline.yml` — add `force` input.
- `tests/orchestration/test_pipeline_orchestrator.py` — new tests
  (force re-runs, stage 2 fail skips 3+4, stage 5 gating,
  pipeline_stages on record).

## Step 8 — Gate A (design redteam, fresh subagent)
**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

The reviewer walked the three required spot-checks:
1. Force writes new artifacts vs deletes — Compliant. Forced entries
   flow through `SourceLoader`/`Promoter` (content-addressed); zero
   `rm`/`unlink`/`rmtree` calls in the orchestrator.
2. Stage 5 skipped when zero transcripts reach Stage 4 — Compliant.
   Force alone cannot trigger synthesize.
3. Stage 2→3→4 dependency per transcript — Compliant. `_run_stages_2_to_4`
   returns early on Stage 2 failure with stages 3+4 marked `not_run`,
   while the outer loop iterates transcripts independently.

One Sev-3 (style) note from the reviewer about a redundant
`(force and any_stage4_success_this_run)` operand — cleaned up post-Gate-A
to a single `if any_stage4_success_this_run:` with an explanatory
comment. (Sev-3 not reported per rubric, but addressed for clarity.)

## Step 9 — Test status
| Check | Result |
|---|---|
| pytest collect | 840 (828 baseline + 12 new) |
| pytest run | 829 passed, 11 failed (same 11 pre-existing PDF/cffi failures, zero regressions) |
| audit-governance | exit 0; total_flagged: 0, high: 0 (with DATA_LAKE_PATH set) |
| lint / type-check | N/A (no config) |

## Step 10 — Gate B (diff redteam, fresh subagent)
**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

All four required spot-checks confirmed:
1. YAML still valid — `.github/workflows/run-pipeline.yml` parses
   cleanly with `yaml.safe_load`.
2. `pipeline_stages` present in schema 1.3.0 with correct enum.
3. `test_stage2_failure_skips_stages_3_and_4` present, asserts both
   that runners 3+4 are NOT called AND that synthesize is skipped.
4. `[force]` prefix logged in CLI run_pipeline (lines printing
   `Mode: FORCE RE-PROCESS (all phases)` banner and `[force] Running:`
   per-transcript).

Sev-1 hazards re-checked:
- No deletions (test_force_never_deletes_existing_artifacts asserts
  prior `source_record.json` byte content is preserved).
- Stage 5 with zero successes (test_force_synthesize_not_run_when_zero_stage4_success).
- Deterministic stage order (stages run 2→3→4 inside `_run_stages_2_to_4`;
  transcripts iterate in `sorted(transcripts_dir.iterdir())` order).

Reviewer noted one design observation (not a blocker): operators
expecting end-to-end re-synthesize over already-processed data must use
`--force` to get one. This matches the documented "at least one
transcript reached Stage 4 success this run" rule.

---

# fix/orchestrator-stage-status — Progress

## Step 1 — Inventory

| Item | Result |
|---|---|
| Branch (harness-assigned) | `claude/fix-orchestrator-status-cfO3m` (PR vs `main`) |
| pytest collect (baseline) | 840 |
| pytest run (baseline) | 829 passed, 11 failed (pre-existing PDF/cffi) |

### Where the bug lives

`src/spectrum_systems_core/orchestration/pipeline_orchestrator.py` —

1. `_default_extract_stories_runner` runs `Chunker → StoryExtractor →
   StoryEval → StoryworthyFilter` directly (in-process, not subprocess).
   `Chunker` is deterministic and writes `stories/chunks.jsonl` with no
   external dependencies. `StoryExtractor` is the one that calls the
   Anthropic API to produce `stories/candidates.jsonl`. **In production
   the API call failed** → runner returned `status="failure"` →
   orchestrator reported `sty✗` for all 13 transcripts. But `chunks.jsonl`
   was successfully written before the failure.
2. `_run_one_stage` (line 871) reads the runner's return dict and only
   checks `result.get("status") == "success"`. There is no artifact-
   existence cross-check. A runner that returns `failure` is always
   recorded as stage failure even when the on-disk artifact proves
   partial success.
3. `_stage2_done` (line 1244) checks `stories/candidates.jsonl` for
   idempotency. Since the StoryExtractor never wrote it, the next run
   re-attempts Stage 2 — which is correct, but it does mean the
   orchestrator never gives credit for the chunks.jsonl that was written.
4. Synthesize gates on `any_stage4_success_this_run`. Stage 2 failure
   short-circuits Stages 3+4, so synthesize was always skipped in this
   scenario.

### CLI vs in-process

`_default_extract_stories_runner` does NOT shell out to the `extract-
stories` CLI command. It imports `Chunker`/`StoryExtractor` directly
and reads their `dict` return values. So the "wrong exit-code parsing"
hypothesis from the task description doesn't apply: the orchestrator
reads dicts already, the question is *which* signal is authoritative.

### Decision: apply Fix C (artifact-existence as primary)

Per task spec, treat the on-disk artifact as the primary success
signal:

- Stage 2 artifact-evidence: `processed/<family>/<sid>/stories/chunks.jsonl`
  (Chunker output — deterministic, no API)
- Stage 3 artifact-evidence: any of
  `processed/<family>/<sid>/knowledge/{concepts,themes,analogies}.jsonl`
  (already what `_stage3_done` checks)
- Stage 4 artifact-evidence: `processed/<family>/<sid>/paper/claims.jsonl`
  (already what `_stage4_done` checks)

Snapshot artifact existence BEFORE the runner runs, then re-check after.
Discrepancies between runner result and artifact change get a printed
warning. Sev-1 guard: a runner failure with the artifact *already*
present (pre-existed, no new output) is still a failure — we cannot
treat a stale artifact as new evidence.

Synthesize gating changes from `any_stage4_success_this_run` to
`any_stage2_success_this_run` (per task: "Synthesize now runs if any
stage 2 artifacts exist"). Existing test `test_force_synthesize_not_run_
when_zero_stage4_success` still passes because zero Stage 2 success
=> zero Stage 4 success.

## Step 2-4 — Fix applied

| File | Change |
|---|---|
| `src/spectrum_systems_core/orchestration/pipeline_orchestrator.py` | New helper `_stage2_artifact_exists` (chunks.jsonl); rewrote `_run_one_stage` to snapshot pre/post artifact existence and apply the decision matrix; `_run_stages_2_to_4` now returns `stage2_success`; `_run` gates synthesize on `any_stage2_success_this_run`. |
| `tests/orchestration/test_pipeline_orchestrator.py` | `_stage_runner_factory` Stage 2 now writes both `chunks.jsonl` and `candidates.jsonl`; added 5 new tests for artifact-existence behavior. |

Applied Likely Fix C (artifact-existence as primary success signal).
Fix A (subprocess) and Fix B (exit-code parsing) did not apply because
the runner is already invoked as an in-process function and reads its
return dict directly. Fix D (direct function call) was already in place.

## Step 5 — Tests added (5 new)

1. `test_story_extraction_success_detected_by_artifact_existence`
2. `test_story_extraction_failure_when_no_artifact`
3. `test_synthesize_runs_when_stories_exist`
4. `test_summary_shows_correct_success_count`
5. `test_stale_artifact_does_not_mask_runner_failure` (Sev-1 guard)

## Step 6 — Gate A (design redteam, fresh subagent)

**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

Reviewer walked the four required spot-checks and confirmed:
1. Stale artifact + runner failure does NOT mask as success
   (`artifact_pre_existed` snapshot + `artifact_produced_this_run`
   flag; covered by `test_stale_artifact_does_not_mask_runner_failure`).
2. Stage 2 idempotency marker is `candidates.jsonl` (full-completion);
   artifact-evidence marker is `chunks.jsonl` (partial-completion).
   A "chunks-without-candidates" state correctly retries next run.
3. Synthesize on data without claims is the task spec's stated
   requirement and is enforced by `test_synthesize_runs_when_stories_exist`.
4. `_stage_runner_factory` writes both `chunks.jsonl` and
   `candidates.jsonl` on Stage 2 success.

## Step 7 — Test status post-fix

| Check | Result |
|---|---|
| pytest collect | 845 (840 baseline + 5 new) |
| pytest run | 834 passed, 11 failed (same 11 pre-existing PDF/cffi failures) |
| audit-governance (`DATA_LAKE_PATH=data-lake`) | exit 0; total_flagged 0; high 0 |
| lint / type-check | N/A — no config |

## Step 8 — Gate B (diff redteam, fresh subagent)

**Verdict: no blocking findings (zero Sev-1, zero Sev-2).**

Reviewer confirmed:
- Sev-1 stale-artifact guard present (`artifact_pre_existed` /
  `artifact_post_exists` pair).
- All three discrepancy warnings printed (cli_success_artifact_missing,
  cli_failure_artifact_produced, stale_artifact_not_treated_as_success).
- Test factory writes both chunks.jsonl AND candidates.jsonl.
- All four required tests present plus the additional stale-artifact
  Sev-1 test.
- Existing `test_force_synthesize_not_run_when_zero_stage4_success`
  semantics preserved (verified by full-suite pass).

