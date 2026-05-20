# Rollback Contracts

Document ID: SSC-ROLLBACK-001
Status: Binding for any change with a versioned schema or governance gate
Scope: Spectrum Systems Core

This file documents the rollback path for every governance change that
introduces a new schema version, a new gate, or a new diagnostic artifact.
Every entry must answer two questions:

1. What does an operator do to revert the change?
2. What happens to artifacts already produced under the change?

Every Claude Code session that adds a new schema version or a new
gate MUST add a corresponding entry here BEFORE opening the PR.

---

## Phase 1 — verbatim span grounding (PR #XXX)

### What this change adds

- `meeting_minutes` schema bumped from 1.3.0 to 1.4.0.
- Every item-type sub-schema declares a `grounding_mode` discriminator
  with value `verbatim` or `turn_aggregate`.
- Verbatim items add optional `source_quote`, `quote_offset_normalized`,
  `quote_offset_original` fields. Turn-aggregate items add optional
  `source_turn_ids`.
- New module `src/spectrum_systems_core/grounding/` (normalization).
- New module `src/spectrum_systems_core/promotion/gate.py` with
  `verify_grounding()` and `grounding_rejection_report_payload()`.
- New artifact type `grounding_rejection_report` (diagnostic, never
  promoted, never indexed).
- New CLI flag `--allow-mixed-schema` on `scripts/compare_opus_haiku.py`
  (CLI-only — not env, not config).
- Comparison engine re-verifies grounding on 1.4.0 haiku artifacts and
  sets `tainted: true` when re-verification fails.

### To roll back

1. Revert the schema PR. Pre-1.4 artifacts in the data lake remain valid
   because they declare `schema_version: 1.3.x` (or earlier) and the
   pre-1.4 schema validates them unchanged.
2. The grounding gate becomes a no-op for 1.3.x artifacts automatically
   because callers only invoke it for 1.4.0 producers — pre-1.4
   workflows never enter the new code path.
3. Existing 1.4.0 artifacts in the data lake remain READABLE even after
   rollback: the comparison engine and downstream consumers tolerate the
   extra `grounding_mode` / `source_quote` / `quote_offset_*` /
   `source_turn_ids` keys because the comparator reads them only when
   present. If the strict schema validator runs against a 1.4.0 artifact
   under a reverted (pre-1.4) schema, it will fail
   `additionalProperties: false` — operators should NOT re-validate
   pre-existing 1.4.0 artifacts after rollback. The comparator does NOT
   re-validate stored artifacts on read, so the rollback does not
   spontaneously break any in-flight workflow.
4. The correction miner's hallucination-pattern handling becomes a no-op
   because no new `grounding_rejection_report` artifacts will be
   produced; any existing diagnostics on disk remain present as inert
   files (the miner's reader tolerates an empty set).
5. The `--allow-mixed-schema` CLI flag disappears with the revert.
   Any caller relying on it must drop the flag from its invocation.

### Data migration required for rollback

None. Pre-1.4 artifacts validate unchanged; 1.4.0 artifacts in the data
lake are read-only after rollback (their grounding fields become inert
metadata) but they do not need to be deleted, edited, or migrated.

### Verification that the rollback is clean

```bash
# 1. Pre-1.4 artifacts validate unchanged.
python -c "
import json
from spectrum_systems_core.validation import validate_artifact, _load_schema
_load_schema.cache_clear()
art = json.loads(open('<pre-1.4-artifact-path>').read())
validate_artifact(art, 'meeting_minutes')
print('OK')
"

# 2. Pre-1.4 workflows still produce a promoted artifact end-to-end.
python -m pytest tests/test_golden_transcripts.py -q
```

If either step fails, the rollback has not actually completed — fix
forward rather than re-revert.

---

## Phase 2 — eval-path alignment + tolerance budget + calibration mode (PR #193)

### What this change adds

- New module `src/spectrum_systems_core/pipeline/governed_run.py`
  providing `governed_pipeline_run` — the single execution path for
  extraction → schema_validate → grounding_gate → compare. Both the
  correction miner (`scripts/correction_miner.py`) and the production
  CLI (`src/spectrum_systems_core/cli.py meeting-minutes-llm`) route
  through it so a measurement-layer drift between the two callers
  cannot recur (the 4.1-point F1 gap that motivated Phase 2).
- New module `src/spectrum_systems_core/calibration/budget.py` with
  `get_variance_budget`, `get_promotion_threshold`, and
  `is_in_calibration_mode`. The miner's `should_promote` reads the
  threshold from the budget; calibration mode blocks promotion until
  one non-legacy comparison_result exists.
- New contract `docs/contracts/tolerance_budget.json` and schema
  `src/spectrum_systems_core/schemas/tolerance_budget.schema.json`.
  The `current_promotion_buffer` bound (0.02 ≤ x ≤ 0.10) is enforced
  by the JSON Schema at write time, not by code.
- New artifact `pipeline_invocation_log` (schema 1.0.0). Diagnostic
  only — never promoted, never indexed. Written to
  `processed/meetings/<source_id>/diagnostics/` by
  `governed_pipeline_run`. Expires 30 days after `started_at`.
- New CI script `scripts/verify_rollback_contracts.py` that asserts
  every PR adds a `rollback_contracts.md` entry referencing the PR
  number, at least one changed file path, and a `verification_command`
  from the whitelist (`pytest <path>`, `python scripts/<name>.py`,
  `python -m spectrum_systems_core.<module>`).
- New weekly reconciler `scripts/reconcile_invocation_logs.py` that
  surfaces comparisons without a paired invocation log to
  `reconciliation_gaps.jsonl`. Non-blocking.
- New comparison-engine gates in `scripts/compare_opus_haiku.py`:
  - `prompt_drift_post_merge` — fires when the production artifact's
    `prompt_content_hash` disagrees with the most recent miner run's
    `expected_post_merge_prompt_hash`. Off when the miner has never
    run for the source.
  - Schema-version coherence (Step 2.7): a 1.4.0 Haiku artifact
    diffed against a pre-1.4 baseline halts UNLESS a baseline at the
    matching version is on disk. The `--allow-mixed-schema` flag from
    PR #192 stays as a last-resort operator override.
- New CI gate `tests/pipeline/test_call_graph_single_path.py` that
  walks the AST of every module under `src/spectrum_systems_core/`
  and asserts every function that takes a prompt + transcript pair
  either IS `governed_pipeline_run` or calls into it. A synthetic
  alternate path WILL fail the gate (self-test included).
- New optional `extraction_config` block on `meeting_minutes` payload
  `provenance` (schema 1.4.0 — additive). Captured by
  `governed_pipeline_run` so a run can be re-executed from the
  artifact alone. Legacy artifacts (no `extraction_config`) are
  classified `legacy_eval: true` and excluded from the tolerance-
  budget per-source variance computation.
- New `legacy_eval` field on `comparison_result` (top-level
  additive). Stamped by `build_comparison_artifact` via
  `is_legacy_eval(haiku_artifact)`.
- Calibration mode (Step 2.8): when fewer than one non-legacy
  comparison_result exists for a source, the correction miner can
  generate candidates and write PRs but `promoted: false` — the PR
  description carries `calibration: this candidate is not yet
  promoted — pending baseline run`.

### To roll back

1. Revert the Phase 2 PR. The new modules
   (`pipeline/governed_run.py`, `calibration/budget.py`, the new
   schemas) disappear; the comparison engine's `legacy_eval` field
   reverts to a no-op (legacy `comparison_result` consumers tolerate
   the missing key — the read path uses `.get("legacy_eval")`).
2. Pre-2 `meeting_minutes` artifacts WITHOUT the
   `payload.provenance.extraction_config` block remain valid against
   the 1.4.0 schema because the field is optional. Phase-2 artifacts
   WITH the block become inert — the field is unread by the post-
   revert miner.
3. The correction miner's `should_promote` is no longer importable
   after the revert; the production gate falls back to the legacy
   `exceeds_promotion_threshold(delta_f1) > 0.05` check that this
   PR preserves verbatim. No fixture changes are required.
4. `docs/contracts/tolerance_budget.json` becomes an inert config
   file. Operators may delete it for cleanliness.
5. The `pipeline_invocation_log` diagnostic schema and the
   `tolerance_budget` schema disappear. Existing on-disk
   `pipeline_invocation_log__*.json` files remain as inert
   diagnostics (same lifecycle as `grounding_rejection_report` in
   PR #192's rollback contract).
6. The CI workflow step that runs `verify_rollback_contracts.py`
   disappears with the revert. Subsequent PRs that omit the
   rollback_contracts.md entry are no longer blocked by automated
   means; reviewer enforcement returns to the manual CLAUDE.md
   process.

### Data migration required for rollback

None. The Phase 2 additions are additive at every layer:

- The `extraction_config` block is optional in the `meeting_minutes`
  schema; pre-Phase-2 artifacts validate unchanged.
- The `legacy_eval` field is optional in `comparison_result`;
  consumers tolerate its absence.
- The `pipeline_invocation_log` artifacts have a 30-day TTL and the
  reconciler is non-blocking; expired logs may be deleted by the
  operator at any time.

### Verification that the rollback is clean

```bash
pytest tests/calibration/
pytest tests/comparison/test_phase2_gates.py
pytest tests/pipeline/
python scripts/reconcile_invocation_logs.py
python scripts/verify_rollback_contracts.py --pr 197 --changed-files src/spectrum_systems_core/pipeline/governed_run.py
```

After revert, the `tests/pipeline/`, `tests/calibration/`, and
`tests/comparison/test_phase2_gates.py` directories are expected to
disappear too. If they remain present and any test fails, the revert
is incomplete — fix forward.

---

## Phase 2P — NTIA/DoD glossary injection infrastructure (PR #194)

### What this change adds

- `data/glossary/ntia_dod_spectrum_v1.jsonl` — 57 NTIA/DoD spectrum-policy
  glossary entries.
- `data/glossary/allowed_sources.json` — whitelist of authoritative-source
  prefixes (e.g. `47 CFR`, `ITU-R`, `3GPP`).
- `data/glossary/MANIFEST.json` — sha256 hashes over the canonicalized
  JSONL and the allowed-sources file.
- `src/spectrum_systems_core/schemas/glossary_entry.schema.json` — write-time
  validation gate for each entry.
- `src/spectrum_systems_core/glossary/loader.py` — fail-closed loader,
  matcher, terminology-block formatter.
- `scripts/verify_glossary_manifest.py` — CI gate over both hashes.
- `scripts/verify_glossary_consistency.py` — CI gate over alias /
  definition conflicts.
- `scripts/cli_glossary.py` — thin CLI shell exercising the new flag.
- `--enable-glossary-injection` CLI flag, default `False`. CLI-only:
  environment variables and config files are NOT read.
- Glossary version hash recorded in the chunk-context Terminology
  block (visible to the LLM) and emitted to debug log when the flag
  is enabled.

### To roll back

1. Revert this PR. Reverting deletes the four files under
   `data/glossary/`, the schema, the loader module
   (`src/spectrum_systems_core/glossary/loader.py`), the verifier
   scripts (`scripts/verify_glossary_manifest.py`,
   `scripts/verify_glossary_consistency.py`), the CLI shell
   (`scripts/cli_glossary.py`), the tests under `tests/glossary/`
   (`test_loader.py`, `test_manifest_verification.py`,
   `test_consistency_verification.py`, `test_cli_flag.py`,
   `test_jsonl_artifact.py`), and the workflow CI step. The other
   existing glossary modules (`glossary_builder`, `term_injector`)
   are untouched by this PR and are unaffected by the rollback.
2. No data-lake artifacts are produced by this PR (the flag is `False`
   by default and no production pipeline calls the new loader). No
   data-lake cleanup is required.
3. If any operator independently enabled `--enable-glossary-injection`
   in a local extraction before Phase 2P landed, the resulting
   extraction artifacts have NOT recorded a `glossary_version_hash`
   in their envelope (Phase 2P intentionally does not modify the
   envelope). Those artifacts must be marked `legacy_eval` going
   forward — they cannot be compared against post-Phase-2 artifacts
   without explicit operator acknowledgement that the glossary state
   was unrecorded.

### Data migration required for rollback

None.

### Verification that the rollback is clean

```bash
pytest tests/glossary/
python scripts/verify_glossary_manifest.py
python scripts/verify_glossary_consistency.py
```

After revert, `tests/glossary/test_loader.py`,
`tests/glossary/test_manifest_verification.py`,
`tests/glossary/test_consistency_verification.py`,
`tests/glossary/test_cli_flag.py`, and
`tests/glossary/test_jsonl_artifact.py` are expected to disappear too.
If any of them remain and fail, the revert is incomplete — fix
forward.

### Cross-PR dependency

`depends_on`: (none — the flag default is `False`)

`future_dependency`: a separate PR that flips the default for
`--enable-glossary-injection` or enables it in production extraction
configurations MUST declare `depends_on: <phase-2-pr-number>` so the
Phase 2 envelope recording lands first. Without that, glossary-enabled
artifacts have no recorded `glossary_version_hash` and cannot be
compared faithfully across versions.

---

## Phase 2R — transcript ingestion quality gate (PR #195)

### What this change adds

- New module `src/spectrum_systems_core/transcript_quality/` —
  `checks.py` (the catalog of gates and severities), `validate.py`
  (pure validator), `_config_loader.py` (config loader + cross-field
  enforcement), `cli_integration.py` (CLI glue).
- New diagnostic artifact `transcript_quality_report` with schema
  `src/spectrum_systems_core/schemas/transcript_quality_report.schema.json`
  (lifecycle mirrors `grounding_rejection_report` — never promoted,
  never indexed).
- New config file `data/transcript_quality_config.json` with schema
  `src/spectrum_systems_core/transcript_quality/config.schema.json`
  (the schema enforces a hard 10M-byte ceiling on
  `hard_max_byte_length`; operators cannot raise it above 10M without
  amending the schema).
- New CLI subcommand `spectrum-core check-transcript` with
  `--transcript-path` / (`--source-id` + `--lake`) mutually-exclusive
  modes.
- New `--enable-pre-flight-check` CLI flag on
  `spectrum-core process-meeting`. CLI-only: not readable from
  environment variables or config files. Default `False`.
- New module `src/spectrum_systems_core/reason_codes.py` — the
  Phase 2R reason-code registry.
- Manifest entry for `transcript_quality_report` appended to
  `docs/architecture/artifact_manifest.md`.

### To roll back

1. Revert the PR. The new modules
   (`src/spectrum_systems_core/transcript_quality/`,
   `src/spectrum_systems_core/reason_codes.py`) disappear; the new
   schema `transcript_quality_report.schema.json` and config file
   `data/transcript_quality_config.json` disappear with them.
2. The `spectrum-core check-transcript` subcommand and the
   `--enable-pre-flight-check` flag on `process-meeting` disappear
   with the revert. The default behaviour of `process-meeting` is
   unchanged because the flag was opt-in (default `False`).
3. Existing `transcript_quality_report__*.json` diagnostic files in
   the data lake remain on disk. They are safe to leave: no
   downstream consumer reads them in this PR. Operators may delete
   them for cleanliness.

### Data migration required for rollback

None. The Phase 2R additions are additive at every layer:

- The validator is a new module; no existing code path imports it.
- The diagnostic schema is new; no existing artifact carries the
  `transcript_quality_report` type.
- The config file is new; no existing module reads it.
- The `--enable-pre-flight-check` flag defaults to `False`, so the
  extraction path is byte-for-byte unchanged for operators who do
  not opt in.

### Verification that the rollback is clean

```bash
pytest tests/transcript_quality/
python scripts/verify_rollback_contracts.py --pr 195 --changed-files src/spectrum_systems_core/transcript_quality/validate.py
```

After revert, the `tests/transcript_quality/` directory is expected
to disappear too. If it remains present and any test fails, the
revert is incomplete — fix forward.

### Future dependency

This PR ships with `--enable-pre-flight-check` default `False`. A
follow-up PR (after Phase 2 lands and `governed_pipeline_run` is the
single execution path) will flip the default to `True`. A second
follow-up (after Phase 2Q lands) will surface the presence of
`transcript_quality_report__*.json` files in `spectrum-core status`
output. Both follow-up PRs MUST add their own rollback contract
entries in this file.

---

## Phase 3 — glossary production wiring + measurement (PR #196)

### What this change adds

- `pipeline.governed_pipeline_run` now loads the NTIA/DoD glossary
  when `enable_glossary_injection=True` (the new default) and
  prepends a per-batch Terminology block to the user message via
  `workflows.meeting_minutes_llm._prepend_glossary_block`.
- `ExtractionConfig` gains three optional Phase-3 fields:
  `glossary_version_hash`, `glossary_tokens_added`,
  `tainted_glossary_drift`. The hash + tokens pair is enforced
  present-together-or-absent-together by the new
  `pipeline.governed_run.validate_glossary_metadata_consistency`
  (the JSON Schema cannot natively express the rule).
- `meeting_minutes.schema.json` (no version bump — the new keys
  attach to the existing optional `extraction_config` object).
- New artifact `tolerance_budget_state` (schema 1.0.0) at
  `data-lake/store/processed/meetings/<source_id>/diagnostics/tolerance_budget_state__<source_id>.json`.
  Never promoted, never indexed.
- `docs/contracts/tolerance_budget.json` bumped to schema_version
  `1.1.0`; `per_source_budgets` removed (now lives in the per-source
  state artifact). The bound on `current_promotion_buffer` still
  applies.
- `calibration.budget.update_per_source_state` post-extraction hook,
  called by `governed_pipeline_run` after a non-legacy non-tainted
  comparison is built. Idempotent on the comparison artifact's
  `(source_id, compared_at)` tuple.
- `calibration.budget.get_variance_budget` /
  `get_promotion_threshold` accept a new optional `data_lake_path`
  argument so the reader can pull the per-source state from disk.
  Default `None` returns `global_median_budget` (the previous fallback
  path is preserved).
- New CLI flag pair on `spectrum-core meeting-minutes-llm`:
  `--enable-glossary-injection` and `--disable-glossary-injection`,
  mutually exclusive, CLI-only (env vars are NOT consulted), default
  ON.
- `scripts/run_glossary_measurement.sh` — operator runbook
  (executable). Dispatches the extraction + comparison and calls
  `scripts/print_comparison_delta.py` to print the F1 + delta.
- `scripts/print_comparison_delta.py` — diagnostic helper that reads
  the latest comparison + extraction artifact for a source and
  reports the glossary provenance fields.

### To roll back

1. Revert the PR. The `--enable-glossary-injection` / 
   `--disable-glossary-injection` mutex pair returns to the Phase-2P
   single-flag (`--enable-glossary-injection`, default `False`).
2. Existing extraction artifacts under
   `data-lake/store/processed/meetings/*/meeting_minutes__*.json`
   that carry `glossary_version_hash` / `glossary_tokens_added` /
   `tainted_glossary_drift` remain valid — the fields are optional in
   the `extraction_config` subschema and the comparison engine ignores
   them when the flag is off.
3. Existing `tolerance_budget_state__*.json` files in the data lake
   remain on disk. They are safe to leave: under rollback the budget
   reader stops consulting them (the function call is gone), and the
   files are ignored by index builders (never promoted, never indexed).
4. The `--disable-glossary-injection` flag disappears with the revert.
   Any operator script that relied on it must drop the flag.
5. `docs/contracts/tolerance_budget.json` must be re-edited to
   declare `schema_version: "1.0.0"` and to re-add
   `per_source_budgets: {}` (the schema's old shape). Operators who
   had populated per-source budget data MUST manually port the values
   from the data-lake state artifacts back into the contracts file.

### Data migration required for rollback

If per-source state artifacts have accumulated under the data lake,
their `f1_variance_budget` / `runs_observed` values must be
hand-merged back into the contracts file before the rollback PR is
merged. The operator runbook for that migration is the inverse of
`update_per_source_state` — there is no automatic reverse migration
script (intentional: a rollback is rare, and forcing a manual review
of the values that re-enter the contracts file is the safer default).

### Verification that the rollback is clean

```bash
pytest tests/glossary/test_production_wiring.py tests/calibration/test_per_source_state.py
python scripts/verify_rollback_contracts.py --pr 197
```

After revert, `tests/glossary/test_production_wiring.py` and
`tests/calibration/test_per_source_state.py` are expected to
disappear too. If they remain and fail, the revert is incomplete —
fix forward.

### Cross-PR dependency

`depends_on`: #193 (eval-path alignment — provides the
`extraction_config` block this PR extends and the
`pipeline.governed_pipeline_run` entry point this PR wires into),
#194 (glossary infrastructure — provides the JSONL artifact,
manifest, schema, loader, matcher).

`future_dependency`: a follow-up PR may extend the status CLI's
recommendation enum to surface "glossary enabled vs disabled" per
source. Until then, operators read `extraction_config` from artifact
files directly via `scripts/print_comparison_delta.py`.

### Expected F1 impact

Research synthesis predicts +4–7 F1 points. The actual delta is
measured by the operator AFTER merge via the runbook
(`scripts/run_glossary_measurement.sh`). Both positive and negative
outcomes are acceptable findings — the measurement is the point.

---

## Phase 3P — few-shot examples + negative patterns (PR #XXX)

### What this change adds

- New module ``src/spectrum_systems_core/few_shot/`` with a
  manifest-gated loader (``load_few_shot_registry``), a renderer
  (``build_few_shot_block``), a runtime injector
  (``inject_or_strip_few_shot``), and a missing-reason-rate
  diagnostic (``count_missing_reason_rate``).
- New JSON Schema ``schemas/few_shot_entry.schema.json``
  (additionalProperties: false) with ``id`` / ``example_type`` enum
  / ``speaker_names_stripped: true`` const / 22-array
  ``gold_extraction`` requirement / ``rationale`` minLength=20 /
  ``chunk_text`` maxLength=2000 / ``id`` regex
  ``^fewshot-[0-9]{3}$``.
- New registry artifact ``data/few_shot/examples_v1.jsonl`` (3
  entries) and its hash gate ``data/few_shot/MANIFEST.json``.
- The canonical prompt file
  ``src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md``
  gains two additive sections:
  - **Few-Shot Examples** between ``<!-- FEW_SHOT_BLOCK_BEGIN -->`` /
    ``<!-- FEW_SHOT_BLOCK_END -->`` markers — stripped at runtime by
    ``inject_or_strip_few_shot`` when ``--enable-few-shot`` is OFF.
  - **Do Not Extract (negative patterns)** — NOT marker-wrapped, so
    it is ALWAYS active regardless of the flag (precision guard).
- ``meeting_minutes.schema.json`` gains an optional ``reason`` field
  on object-form ``decisions`` and ``action_items`` items. Backward
  compatible — pre-3P artifacts without ``reason`` still validate.
- ``pipeline_invocation_log.schema.json`` gains an optional
  ``few_shot_reason_missing_rate`` field (number 0.0–1.0). Logged
  only when the rate exceeds 0.20. Pre-3P logs without the field
  still validate.
- New CLI flag pair on ``spectrum-core meeting-minutes-llm``:
  ``--enable-few-shot`` / ``--disable-few-shot`` (mutually
  exclusive). CLI-only — env vars are NOT consulted. Default OFF
  until the operator confirms real-corpus provenance.
- ``scripts/cli_few_shot.py`` — CLI shell that prints the production
  prompt with the section either present or stripped (used by
  ``tests/few_shot/test_cli_flag.py``).
- ``scripts/verify_fewshot_speaker_names.py`` — heuristic scanner
  flagging suspected un-stripped speaker names.
- ``scripts/verify_fewshot_no_regression.py`` — negative-transfer
  guard. Reads comparison_result artifacts under a data-lake root,
  pairs the most recent pre/post comparisons per source by
  prompt_content_hash, and exits 1 when any source's F1 drops by
  more than 5 points.

### To roll back

1. Revert the PR.
2. The Few-Shot Examples and Do Not Extract sections leave the
   prompt file (the revert handles this automatically).
3. The ``reason`` field in ``meeting_minutes.schema.json`` remains
   optional in the schema (backward-compat). Existing extraction
   artifacts with ``reason`` fields remain valid. The field causes
   no harm if left.
4. The ``few_shot_reason_missing_rate`` field in
   ``pipeline_invocation_log.schema.json`` remains optional.
   Existing logs that recorded it remain valid.
5. Close any open correction-miner candidate PRs immediately. Their
   ``expected_post_merge_prompt_hash`` is now stale and the
   comparisons they reference were taken against a stripped prompt
   that no longer exists in production.
6. The data registry under ``data/few_shot/`` remains on disk but
   is unused — safe to leave as the audit trail for the (closed)
   experiment.

### Data migration required for rollback

None. All schema changes are additive; the few-shot block is
flag-gated default OFF; the regression guard reads existing on-disk
artifacts non-destructively.

### Verification that the rollback is clean

```bash
pytest tests/few_shot/ -q
python scripts/verify_fewshot_no_regression.py --lake tests/fixtures/few_shot_regression_passing/ --current-hash POST_HASH_SENTINEL
```

After revert, ``tests/few_shot/`` is expected to disappear too. If
it remains and fails, the revert is incomplete — fix forward.

### Cross-PR dependency

``depends_on``: #193 (eval-path alignment — provides
``prompt_content_hash`` on the comparison artifact, which the
negative-transfer guard pairs pre/post comparisons by); #196
(Phase 3 — provides the glossary mutex flag pair pattern this PR
mirrors and the ``governed_pipeline_run`` entry point this PR
threads ``enable_few_shot`` and ``few_shot_reason_missing_rate``
through).

``operator_action_on_merge``: Close any open correction-miner
candidate PRs immediately after merge. Their
``expected_post_merge_prompt_hash`` no longer matches production
once this PR lands.

``future_dependency``: A follow-up PR flips ``--enable-few-shot``
default to True after the operator confirms:
1. All synthetic entries in ``examples_v1.jsonl`` are replaced with
   real corpus data (``synthetic: false`` on every entry and
   ``has_synthetic_entries: false`` in the manifest), AND
2. ``verify_fewshot_no_regression.py`` passes on post-merge
   comparison artifacts in the live data lake.

### Expected F1 impact

Research synthesis predicts +8–16 F1 points combined for few-shot
examples + negative patterns. Actual measurement is gated on
operator action: the flag stays OFF in this PR, so production F1
does not change. The follow-up enable PR runs the runbook and
measures the delta.

---

## How to add a new entry

When a future PR adds a versioned schema, a new gate, or a new
diagnostic artifact, append a section to this file BEFORE merging.
Each section MUST contain:

1. **What this change adds** — bullet list of new files / schemas /
   gates / CLI flags.
2. **To roll back** — numbered steps an operator follows to revert.
3. **Data migration required for rollback** — usually "None"; if any
   migration is needed, document it explicitly.
4. **Verification that the rollback is clean** — copy-pasteable
   commands that prove the revert worked.

A PR that touches schema or governance without updating this file is
rejected by the pre-PR self-review pass (CLAUDE.md, Claude Code
Execution Standard).
