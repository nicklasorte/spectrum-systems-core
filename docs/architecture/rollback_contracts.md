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

---

## Phase 4 — corpus ingestion + status corpus mode (PR #197)

### What this change adds

- `data/corpus/manifest.json` — single source of truth for the 13
  transcripts in the corpus.
- `src/spectrum_systems_core/schemas/corpus_manifest.schema.json` —
  schema for the manifest (`additionalProperties: false`,
  enum-restricted `meeting_type` and `ingestion_status`).
- `src/spectrum_systems_core/corpus/manifest_loader.py` — loader
  with hash verification + custom uniqueness / supersedes checks.
- `src/spectrum_systems_core/corpus/ingest.py` — implementation of
  the `ingest-corpus` CLI.
- `src/spectrum_systems_core/corpus/status.py` — implementation of
  the `status --corpus` CLI rollup.
- `src/spectrum_systems_core/schemas/status_report.schema.json` —
  schema for the rollup. The `state` and `recommendation` enums are
  introduced fresh; subsequent PRs may extend additively without
  redefining existing values.
- Two new subcommands on `spectrum-core` (`ingest-corpus` and
  `status --corpus`).
- `docs/contracts/tolerance_budget.json` and
  `src/spectrum_systems_core/schemas/tolerance_budget.schema.json`
  bump from `1.1.0` to `1.2.0`, adding the required `bootstrap_variance`
  field bounded `[0.02, 0.15]`.
- `src/spectrum_systems_core/calibration/budget.py`
  `get_variance_budget` and `get_promotion_threshold` add the third
  fallback tier (`bootstrap_variance`) used when no source in the
  lake yet has accumulated `runs_observed >= 3`.
- `data/cost_constants.json` and
  `src/spectrum_systems_core/schemas/cost_constants.schema.json` —
  per-model API pricing placeholders pending operator verification.
- `src/spectrum_systems_core/cost/estimator.py` — pure cost estimator
  used by `baseline-opus --all --confirm-cost` (the baseline-opus
  CLI itself is deferred — see "Opus prompt status" below).

### Opus prompt status

Step 4.4 (`baseline-opus` subcommand) is DEFERRED in this PR. The
canonical Opus baseline prompt was not found at the expected path
`src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md`
when Phase 4 ran its first task. A follow-up Phase 4a PR must create
the prompt and regenerate the Dec 18 baseline before the
`baseline-opus` CLI can be wired up. Phase 4b will then implement
Step 4.4 and expand the rollback contract to cover it.

The cost estimator and the `status --corpus` recommendation
`run_baseline_opus` ship in this PR because they are not coupled to
the Opus prompt file's existence.

### To roll back

1. Revert the PR. The two new CLI subcommands disappear; the
   corpus manifest, schema, and loader disappear with them.
2. `data/corpus/manifest.json` is removed from the repo. Existing
   data-lake artifacts produced by the Phase 4 ingest CLI
   (`source_record.json`, `transcript_quality_report__*.json`)
   remain on disk under
   `processed/meetings/<source_id>/` because the data lake is
   append-only — they are simply no longer rewritten by an
   `ingest-corpus` invocation.
3. The `status_report` schema and the `status --corpus` mode
   disappear together. A future PR that wants to bring the rollup
   back must redeclare both — the enum values introduced here
   (`pending`, `validated`, `under_review`, `quarantined`,
   `baseline_complete`, `comparison_complete`, `superseded`,
   `orphaned_in_lake` for `state`; `run_ingest_corpus`,
   `force_review_quarantined`, `investigate_orphan_in_lake`,
   `run_baseline_opus`, `run_comparison`, `none` for
   `recommendation`) are part of the rollback's contract surface.
4. The `bootstrap_variance` field is removed from the contracts
   file; the schema reverts to `1.1.0` and stops requiring it.
   `calibration.budget.get_variance_budget` reverts to its
   Phase-3 two-tier fallback (per-source -> global_median).
5. `data/cost_constants.json`, the cost schema, and the cost
   estimator module disappear.
6. The `corpus_manifest` and `cost_constants` schema files
   disappear; any tooling that imported them must drop the import.

### Data migration required for rollback

None. The data lake is append-only; existing source_records and
diagnostic reports remain readable. Future runs simply stop touching
them.

### Verification that the rollback is clean

```bash
pytest tests/corpus/ tests/cost/ tests/calibration/test_bootstrap_variance.py
python scripts/verify_rollback_contracts.py --pr TBD
```

After revert, the test files under `tests/corpus/`, `tests/cost/`,
and `tests/calibration/test_bootstrap_variance.py` are expected to
disappear. If they remain and fail, the revert is incomplete — fix
forward.

### Cross-PR dependency

`depends_on`: #193 (eval-path alignment — provides the
`extraction_config` block the future baseline-opus path consumes),
#195 (transcript-quality validator — the pre-flight gate the ingest
CLI invokes), #196 (glossary production wiring — the prior PR's
recommendation about a future status-CLI extension is honoured by
this PR's additive enum, not by editing PR #196's surface).

`future_dependency`: a follow-up PR (Phase 4a / 4b) will create the
Opus baseline prompt and add the `baseline-opus` CLI subcommand
gated behind `--confirm-cost`. Until then, the cost estimator and
the `run_baseline_opus` recommendation are present but no command
consumes them.

### Constraint compliance

This PR makes NO modifications to:
- `src/spectrum_systems_core/pipeline/governed_run.py` (Phase 2/3)
- `src/spectrum_systems_core/schemas/meeting_minutes.schema.json` (Phase 2)
- `scripts/compare_opus_haiku.py` (Phase 2)
- `scripts/correction_miner.py` (Phase 2)
- `src/spectrum_systems_core/grounding/` (Phase 1)
- `src/spectrum_systems_core/glossary/` (Phase 2P)
- `src/spectrum_systems_core/transcript_quality/` (Phase 2R; allowed:
  the ingest CLI imports `validate` and calls it, but does not
  modify the module).

The compliance check `tests/corpus/test_constraint_compliance.py`
enforces this against the PR diff.

---

## Phase 3P — few-shot examples + negative patterns (PR #198)

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

## Phase 4a — Opus baseline prompt + baseline-opus CLI (PR #199)

### What this change adds

- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md`
  — the canonical Opus reference prompt. Distinct from the Haiku
  production prompt (`meeting_minutes_llm.md`): the Opus prompt is
  comprehensive ("extract everything") while the Haiku prompt is
  guarded ("extract verbatim, defend against hallucinations").
- `src/spectrum_systems_core/corpus/baseline_opus.py` — implementation
  of the `baseline-opus` subcommand: prompt loader, registry-driven
  model resolution, Anthropic transport seam (stubbable via
  `BASELINE_OPUS_STUB_RESPONSE`), artifact writer, manifest update.
- A new CLI subcommand `spectrum-core baseline-opus`. `--all` mode
  requires `--confirm-cost`; the flag is CLI-ONLY and cannot be
  bypassed via env var.
- `scripts/verify_opus_baseline_consistency.py` — reads existing
  Opus baselines (both the new Phase-4a layout and the legacy
  `reference_baselines/opus_reference_minutes.jsonl` layout) and
  verifies the item count is within the hard range `[90, 125]`. Item
  counts outside the reference range `[100, 112]` emit a WARNING but
  exit 0 — the operator decides whether to accept. A missing
  `prompt_content_hash` (pre-Phase-2 legacy artifact) is a WARNING,
  never a failure.
- The Opus baseline writes a `meeting_minutes_opus__<timestamp>.json`
  artifact at `processed/meetings/<source_id>/`. The artifact type
  `meeting_minutes_opus` is new and is read by
  `corpus.status._has_opus_baseline` (the glob existed before this
  PR; this PR is the producer).
- A diagnostic JSONL marker `baseline_opus_history.jsonl` is appended
  under `processed/meetings/<source_id>/diagnostics/` on every
  successful baseline run. The marker is informational — it does
  NOT advance the per-source variance budget (which is fed by
  Haiku-vs-Opus comparison F1s, not by the Opus baseline itself).

### Opus prompt status: resolved

Phase 4 (PR #197) deferred Step 4.4 because the Opus prompt did not
exist. This PR canonicalises the prompt, computes its sha256, and
wires the CLI. Future PRs that bump the prompt MUST update the
in-repo file and re-run `verify_opus_baseline_consistency.py`
against the resulting baseline; the hash mismatch is INFO, not a
failure (a differing hash is expected when the prompt evolves).

### To roll back

1. Revert the PR.
2. The `meeting_minutes_opus.md` prompt file is removed.
3. The `baseline-opus` CLI subcommand is removed.
4. The `scripts/verify_opus_baseline_consistency.py` script is
   removed.
5. Any `meeting_minutes_opus__*.json` artifacts produced in the
   data lake by `baseline-opus` runs remain on disk (the data
   lake is append-only from core's perspective). They are
   backward-compatible with the comparison engine — the comparison
   engine reads the legacy `opus_reference_minutes.jsonl` and is
   unaffected by the new envelope format.
6. The `baseline_opus_history.jsonl` diagnostic files remain on
   disk; they are append-only diagnostics with no consumer outside
   this PR.
7. The manifest's `ingestion_status` fields updated by this CLI
   remain at `baseline_complete`; reverting the code does not
   revert the manifest state. The operator must manually reset if
   desired (e.g. via `bootstrap_hash` after editing the observed
   block).

### Data migration required for rollback

None. The data lake is append-only; `meeting_minutes_opus__*.json`
artifacts and `baseline_opus_history.jsonl` markers remain readable.
Future runs simply stop touching them.

### Verification that the rollback is clean

```bash
pytest tests/corpus/test_baseline_opus.py
python scripts/verify_opus_baseline_consistency.py --lake <data-lake-path>
```

After revert, both test files are expected to disappear. The
verifier script also disappears. If either remains and fails, the
revert is incomplete — fix forward.

### Cross-PR dependency

`depends_on`: #197 (Phase 4 — corpus manifest, ingest CLI, cost
estimator; the cost estimator is the dependency for the `--all
--confirm-cost` summary print-out).

### Operator action after merge

1. Run `spectrum-core baseline-opus --source-id m-2025-12-18-7ghz-downlink-tig-kickoff --lake <data-lake-path>`
   to regenerate the Dec 18 baseline with the canonicalised prompt.
2. Compare the new baseline's item count to 106 (the legacy
   baseline). If within ±10 items (i.e. ~96–116), the prompt is
   consistent with the prior run.  If outside this band, audit the
   new baseline before running on other sources.
3. Run `python scripts/verify_opus_baseline_consistency.py --lake <data-lake-path>`
   against the new artifact and confirm a green exit (0).
4. Once Dec 18 is verified, run `spectrum-core baseline-opus --all --confirm-cost --lake <data-lake-path>`
   to baseline the other 12 sources.

### Constraint compliance

This PR makes NO modifications to:
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  (the Haiku production prompt is Haiku territory).
- `src/spectrum_systems_core/pipeline/governed_run.py` (Phase 2).
- `scripts/compare_opus_haiku.py` (the comparison engine, Phase 2).
- `scripts/correction_miner.py` (the correction miner core, Phase 2).
- `src/spectrum_systems_core/grounding/` (Phase 1).
- `src/spectrum_systems_core/glossary/` (Phase 2P).
- `src/spectrum_systems_core/transcript_quality/` (Phase 2R).
- `src/spectrum_systems_core/extraction/` few-shot infrastructure
  (Phase 2 extensions).

A grep against the diff in the PR description proves these.

---

## Phase 5 — Sonnet model wiring + three-way comparison measurement (PR #200)

### What this change adds

- `--model {haiku,sonnet,sonnet-unconstrained,opus}` flag on
  `spectrum-core meeting-minutes-llm` (CLI-only; env vars NOT
  consulted). Default: `haiku`, byte-identical to pre-Phase-5
  behaviour.
- `--repeat N` and `--confirm-cost` flags on the same CLI. `N > 1`
  requires `--confirm-cost` fail-closed.
- `--dry-run` flag on the same CLI: prints the resolved
  (model_id, prompt_path, prompt_variant) and exits without an
  artifact / API call.
- `--show-all-models` flag on `spectrum-core status --corpus`
  (Phase-4 status CLI). Default OFF — output is byte-identical
  to Phase 4. When ON, rows carry three additive optional
  fields: `haiku_latest_f1`, `sonnet_latest_f1`,
  `opus_item_count` (each `null` when the corresponding
  artifact has not been produced).
- New module
  `src/spectrum_systems_core/workflows/model_selection.py` —
  single source of truth for the `--model` resolution
  (model_id, prompt_path, prompt_variant).
- `extraction_config.prompt_variant` (optional enum field) added
  to `meeting_minutes.schema.json`. Four values: `production_haiku`,
  `haiku_prompt_with_sonnet_model`, `opus_prompt_with_sonnet_model`,
  `opus_baseline`. Pre-Phase-5 artifacts omit the field; the
  comparison engine defaults a missing value to `production_haiku`.
- `ExtractionConfig.prompt_variant` field on the dataclass in
  `pipeline/governed_run.py` and a matching argument on
  `governed_pipeline_run`.
- `claude-sonnet-4-6` entry in `data/cost_constants.json` and
  `DEFAULT_SONNET_OUTPUT_TOKENS = 4000` in
  `cost/estimator.py`.
- `haiku_prompt_variant` / `sonnet_prompt_variant` optional
  enum fields on `comparison_result.schema.json` so the
  comparison engine surfaces the variant identity of each
  candidate.
- `scripts/run_sonnet_baseline.sh` — operator-facing runbook
  (dispatch extraction → dispatch comparison → print delta).
- `scripts/print_three_way_delta.py` — pure formatter that
  reads the most recent three-way comparison artifact for a
  source and prints F1/recall/precision plus the delta.
- `docs/architecture/phase5_three_way_audit_report.md` —
  Step 5.6 audit report enumerating the comparison-engine code
  paths that were touched and which were deferred.
- `tests/sonnet/` — new test module covering the model
  resolver, the CLI flag wiring, the schema enum, and the
  status `--show-all-models` flag.
- `tests/cost/test_sonnet_pricing.py` — pricing sanity tests.
- `tests/comparison/test_three_way_audit.py` — the Step 5.6
  three-way audit's behavioural tests.

### Model string verification

The four Phase 5 model strings live in
`src/spectrum_systems_core/workflows/model_selection.py::MODEL_STRINGS`:

- `claude-haiku-4-7`
- `claude-sonnet-4-6`
- `claude-opus-4-7`

These are the Phase 5 spec's nomenclature. The on-disk
`ai/registry/model_registry.json` uses concrete model IDs that may
include dated point releases (e.g. `claude-haiku-4-5-20251001`).
The CLI's `--model` flag OVERRIDES the registry at runtime; the
override values above are flagged here for operator verification
against current Anthropic documentation **before** running the
first live extraction. If a string is wrong, edit only
`workflows/model_selection.py::MODEL_STRINGS` and re-run.

### Sonnet pricing verification

Phase 5 ships `data/cost_constants.json::claude-sonnet-4-6` with:

- `input_per_million_tokens`: 3.00
- `output_per_million_tokens`: 15.00

These match the Phase 5 spec but are flagged as **placeholders
pending operator verification** against current Anthropic
documentation. If incorrect, update `data/cost_constants.json` in
a follow-up commit — the schema validator catches any negative or
out-of-range price.

### To roll back

1. Revert the PR. The `--model` flag returns to a single Haiku
   path. Existing Sonnet artifacts in the data lake remain
   readable (the producer / consumer are decoupled) but the
   `--model sonnet*` and `--model opus` paths disappear.
2. Existing three-way comparison artifacts remain valid; the
   `haiku_prompt_variant` / `sonnet_prompt_variant` fields are
   additive on the comparison_result schema and pre-Phase-5
   readers ignore them.
3. The status CLI's `--show-all-models` flag disappears; default
   behaviour is unchanged.
4. `extraction_config.prompt_variant` becomes an unknown key
   under the reverted schema; artifacts produced under Phase 5
   with the stamp will fail `additionalProperties: false` if
   re-validated. Operators should NOT re-validate pre-existing
   Phase-5 artifacts after revert (same lifecycle rule as the
   Phase 1 grounding fields).
5. The cost estimator's `claude-sonnet-4-6` entry is gone; any
   caller that asked for Sonnet pricing must update to a
   different model_id.

### Data migration required for rollback

None. The data lake is append-only; pre-existing Phase-5 artifacts
keep their `prompt_variant` stamp as inert metadata. Two-way
comparison artifacts continue to validate (the `haiku_prompt_variant`
field is optional in the schema).

### Verification that the rollback is clean

```bash
# 1. Default `--model haiku` still produces a promoted artifact end-to-end.
DATA_LAKE_PATH=$PWD/data-lake python -m spectrum_systems_core.cli \
    meeting-minutes-llm --source-id <source-id>

# 2. The Phase 5 tests are gone.
python -m pytest tests/sonnet/ tests/cost/test_sonnet_pricing.py \
    tests/comparison/test_three_way_audit.py
# Expected: collection error (the directories are gone).

# 3. The unmodified two-way comparison path still produces a
#    byte-identical artifact compared to a pre-Phase-5 input.
python scripts/compare_opus_haiku.py --data-lake $PWD/data-lake \
    --source-id <source-id>
```

If step 3 fails — i.e. the two-way artifact format changes after
revert — the rollback is incomplete; investigate before re-applying
forward.

### Cross-PR dependency

`depends_on`: #193 (eval-path alignment — provides the
`extraction_config` dataclass), #196 (Phase 3 glossary wiring —
the per-source state hook this PR's `--repeat N` flag interacts
with), #197 (Phase 4 corpus — provides the status CLI this PR
extends with `--show-all-models`), and the Phase 4a Opus prompt PR
(`src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md`).
`--model sonnet-unconstrained` and `--model opus` REQUIRE Phase 4a
to have merged; without the Opus prompt on disk both halt with
the resolver's `opus_prompt_not_found_for_sonnet_unconstrained`
or `opus_prompt_not_found` reason code.

`verification_command`: `pytest tests/sonnet/ tests/cost/test_sonnet_pricing.py tests/comparison/test_three_way_audit.py tests/calibration/test_per_source_state.py`

### Operator action after merge

1. Verify `cost_constants.json::claude-sonnet-4-6` pricing
   matches current Anthropic documentation.
2. Verify `model_selection.MODEL_STRINGS` model IDs match
   current Anthropic documentation.
3. Run:
   `scripts/run_sonnet_baseline.sh 7-ghz-downlink-tig-meeting-kickoff---transcript-20251218 haiku-prompt`
   — produces Sonnet's F1 on the Haiku prompt (apples-to-apples).
4. Run:
   `scripts/run_sonnet_baseline.sh 7-ghz-downlink-tig-meeting-kickoff---transcript-20251218 opus-prompt`
   — produces Sonnet's F1 on the Opus prompt (capability).
5. Run: `spectrum-core meeting-minutes-llm --source-id <sid> --model sonnet --repeat 3 --confirm-cost`
   — produces 3 Sonnet runs for variance measurement.
6. Read the three-way comparison artifact. The Sonnet F1 result
   determines the next phase:
   - Sonnet F1 < 50%: build the cascade filter (Phase 6 cascade).
   - Sonnet F1 50–70%: build the cascade with Sonnet as filter.
   - Sonnet F1 > 70%: consider switching primary to Sonnet
     before building the cascade.

Sonnet is opt-in only — no production behaviour changes when
`--model` is omitted.

### Constraint compliance

This PR explicitly does NOT modify:
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md` (Haiku prompt)
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md` (Phase 4a Opus prompt)
- `scripts/correction_miner.py` core miner logic
- `src/spectrum_systems_core/grounding/` (Phase 1)
- `src/spectrum_systems_core/glossary/` (Phase 2P / 3)
- `src/spectrum_systems_core/transcript_quality/` (Phase 2R)
- `src/spectrum_systems_core/few_shot/` (Phase 3P, if present)

The Phase 5 spec explicitly allows the small Step 5.6 audit fixes
to `scripts/compare_opus_haiku.py` (the
`_prompt_variant_of` helper + stamps on the two artifact builders;
< 100 LOC) and the additive `ExtractionConfig.prompt_variant`
extension to `src/spectrum_systems_core/pipeline/governed_run.py`.
The compliance check
`tests/corpus/test_constraint_compliance.py` was updated to
reflect the Phase 5 constraint list and enforces it against the
PR diff.

`future_dependency`: if Sonnet F1 results justify the cascade,
Phase 6 will build a Haiku-extract → Sonnet-filter architecture.
If they justify a model swap, a separate PR flips the default
`--model` from `haiku` to `sonnet`.

---

## Phase 6 — Stage 2 cascade filter (Haiku extract → Sonnet keep/drop) (PR #203)

This PR adds a Stage 2 cascade: after Haiku extracts items, Sonnet
evaluates each item for keep/drop. The cascade is opt-in (flag default
OFF). No production extraction is affected until the operator enables
the flag.

### What this change adds

- `--enable-cascade-filter` / `--disable-cascade-filter` mutually
  exclusive flags on `spectrum-core meeting-minutes-llm` (CLI-only;
  env vars `ENABLE_CASCADE_FILTER` / `DISABLE_CASCADE_FILTER` have
  NO effect — tests/cascade/test_cli_flag.py asserts this). Default
  OFF — pre-Phase-6 behaviour byte-identical.
- New artifact type `meeting_minutes_filtered` (schema 1.0.0). Each
  filtered item is a verbatim subset of the source
  `meeting_minutes` payload; the cascade NEVER invents or mutates
  items.
- New diagnostic artifact `cascade_filter_log` (schema 1.0.0).
  30-day TTL, never promoted, never indexed.
- New JSON Schema `cascade_filter_response.schema.json` describing
  the per-chunk filter response shape; on validation failure the
  executor falls back to CONSERVATIVE pass-through (every item from
  the chunk is kept).
- New cascade module
  `src/spectrum_systems_core/cascade/` (executor.py + __init__.py).
- New cascade prompt
  `src/spectrum_systems_core/workflows/prompts/cascade_filter_sonnet.md`.
- Extension to `cost/estimator.py`:
  `estimate_cascade_cost`, `estimate_extraction_cost_breakdown`
  (returns `CostBreakdown` so the CLI can print both lines when
  cascade is enabled), `load_cascade_confirmation_item_threshold`.
- New constant `cascade_confirmation_item_threshold` in
  `data/cost_constants.json` (default 50). Schema-bounded `[10, 500]`.
- Additive enum extension on `meeting_minutes.schema.json` and
  `comparison_result.schema.json`:
  `production_haiku_with_cascade_filter`. Pre-Phase-6 artifacts
  without the value validate unchanged.
- `--use-cascade-output` flag on `scripts/compare_opus_haiku.py`.
  Default OFF — comparison-engine output is byte-identical to
  pre-Phase-6.
- `CASCADE_FILTER` input on `.github/workflows/debug-llm-extraction.yml`.
  Default false; when true the workflow passes
  `--enable-cascade-filter --confirm-cost`.

### To roll back

1. Revert the PR.
2. The cascade prompt, executor, and CLI flags are removed.
3. Existing `meeting_minutes_filtered` artifacts in the data lake
   remain readable but are no longer producible.
4. The `--use-cascade-output` flag on the comparison engine is removed.
5. The `production_haiku_with_cascade_filter` value in the
   prompt_variant enum is removed; any artifacts using this value
   become invalid (none exist before this PR merges).
6. The `cascade_confirmation_item_threshold` constant is removed
   from `cost_constants.json`.

### Data migration required for rollback

None. The data lake is append-only; pre-existing Phase-6 cascade
artifacts remain on disk as inert files (no reader in the reverted
codebase). The raw `meeting_minutes` artifacts the cascade was
filtering are untouched and remain canonical.

### Verification that the rollback is clean

```bash
pytest tests/cascade/ tests/cost/test_cascade_cost.py \
    tests/comparison/test_use_cascade_output.py
# Expected: collection error (the modules are gone).

# Default extraction still works.
DATA_LAKE_PATH=$PWD/data-lake python -m spectrum_systems_core.cli \
    meeting-minutes-llm --source-id <source-id>

# Default comparison still works byte-identical.
python scripts/compare_opus_haiku.py --data-lake $PWD/data-lake \
    --source-id <source-id>
```

`verification_command`: `pytest tests/cascade/ tests/cost/test_cascade_cost.py tests/comparison/test_use_cascade_output.py`

### Cross-PR dependency

`depends_on`: #192 (verbatim grounding gate — provides per-item
anchoring the cascade uses to splice transcript context into the
filter prompt), #193 (eval alignment — provides
`ExtractionConfig.prompt_variant`), #196 (Phase 3 per-source budget
state — interacts with the cascade's variance signal).

`future_dependency`: a follow-up PR may flip `--enable-cascade-filter`
default to True after the operator confirms cross-source F1
improvement.

### Operator action after merge

1. Verify cascade pricing in `cost_constants.json` matches current
   Anthropic docs. The Phase 6 estimator reuses the
   `claude-sonnet-4-6` row already added in Phase 5; no new pricing
   entry is required.
2. Run `spectrum-core meeting-minutes-llm --enable-cascade-filter --confirm-cost`
   on Dec 18 → produces `meeting_minutes` + `meeting_minutes_filtered`
   artifacts.
3. Run `python scripts/compare_opus_haiku.py --use-cascade-output --data-lake <lake> --source-id <id>`
   → produces F1 for the filtered output vs Opus.
4. Compare the filtered F1 against the 39.5% baseline:
   - `+2 to +5` F1: marginal cascade value; consider tuning the
     filter prompt.
   - `+5 to +10` F1: cascade is the right architecture; scale to
     corpus.
   - `+10+` F1: cascade is a major win; flip default to ON in a
     follow-up PR.
   - `0 or negative`: cascade is filtering too aggressively; adjust
     prompt before proceeding to corpus scale.
5. If filtered F1 > 50%, run on a second source to confirm
   cross-source generalization.

### Conservative failure mode

When Sonnet's filter response fails JSON Schema validation, ALL items
from that chunk are KEPT, not dropped. This preserves recall at the
cost of leaving some false positives in the filtered output.
Operators monitor `chunks_with_invalid_filter_response` in the
`cascade_filter_log` to detect when this happens at scale.

### Constraint compliance

This PR explicitly does NOT modify:
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md` (Haiku prompt)
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md` (Opus prompt)
- `scripts/correction_miner.py` core miner logic
- `src/spectrum_systems_core/grounding/` (Phase 1)
- `src/spectrum_systems_core/transcript_quality/` (Phase 2R)
- `src/spectrum_systems_core/glossary/` (Phase 2P / 3)
- `src/spectrum_systems_core/few_shot/` (Phase 3P)

The constraint compliance test
`tests/cascade/test_constraint_compliance.py` enforces this against
the PR diff.

---

## Stage 2 — per-type source_quote minimum length threshold (opt-in) (PR #214)

This PR adds an opt-in precision-improving threshold to the grounding
gate. A verbatim item whose normalized `source_quote` is shorter than
the per-type minimum is rejected with a new reason code. The threshold
is OFF by default — every existing caller (production promoter,
comparison engine, tests) is byte-identical to pre-Stage-2 behaviour
until it explicitly passes the threshold mapping.

### What this change adds

- New module-level constants in
  `src/spectrum_systems_core/promotion/gate.py`:
  - `MIN_QUOTE_CHARS_SUBSTANTIVE = 30` (substantive verbatim types:
    `decisions`, `action_items`, `commitments`, `claims`, `risks`,
    `position_statement`, `dissent_or_objection`, `procedural_ruling`,
    `precedent_reference`, `external_stakeholder_input`,
    `issue_registry_entry`).
  - `MIN_QUOTE_CHARS_SHORT = 10` (short verbatim types:
    `regulatory_references`, `technical_parameters`,
    `sentiment_indicators`, `glossary_definition`).
  - `DEFAULT_MIN_QUOTE_CHARS_BY_TYPE: dict[str, int]` — the table the
    Stage 2 roadmap recommends. Every entry in `VERBATIM_TYPES` MUST
    appear here (`test_default_min_quote_chars_table_covers_all_verbatim_types`
    pins this invariant). Every value MUST be one of the two tier
    constants (`test_default_min_quote_chars_values_are_substantive_or_short_tier`).
- New optional keyword argument `min_quote_chars_by_type` on
  `verify_grounding` and `grounding_gated_payload`. When supplied, a
  verbatim item whose normalized `source_quote` is shorter than the
  per-type minimum is rejected with reason code
  `grounding_source_quote_too_short`. When `None` (default), the
  threshold check is skipped entirely.
- New reason code `grounding_source_quote_too_short` emitted by the
  gate. The detail string carries the actual length, the expected
  minimum, and the item-type so a reviewer can explain the rejection
  without reading the gate code.
- Ten new unit tests in `tests/promotion/test_grounding_gate.py`
  pinning: table coverage, table tier values, default-off baseline
  preservation, substantive under/at threshold, short under/at
  threshold, unknown-type-not-checked, too-short precedence over
  byte-match, and whole-artifact block when threshold drops rate
  below `GROUNDING_RATE_FLOOR`.

### To roll back

1. Revert this PR. The new constants
   (`MIN_QUOTE_CHARS_SUBSTANTIVE`, `MIN_QUOTE_CHARS_SHORT`,
   `DEFAULT_MIN_QUOTE_CHARS_BY_TYPE`) and the
   `min_quote_chars_by_type` kwarg disappear from `gate.py` and
   `promoter.py`.
2. The reason code `grounding_source_quote_too_short` is no longer
   emitted. Existing `grounding_rejection_report` artifacts on disk
   that carry this reason code remain readable; downstream consumers
   that branch on the reason code default to a generic
   "grounding" interpretation (the comparison engine reads only the
   `artifact_blocked` flag and the per-item count, not the reason
   strings).
3. No data migration required. The threshold was never on in
   production (the default was `None`), so no promoted artifact's
   acceptance decision changes on rollback. Any local experiment that
   set the threshold ON locally simply stops applying it — re-running
   the same input through the rolled-back gate accepts every item that
   had been rejected for being too short.

### Data migration required for rollback

None. The threshold is opt-in: pre-Stage-2 callers do not pass
`min_quote_chars_by_type` and the gate path is byte-identical. No
promoted artifact, no `grounding_rejection_report`, and no
`comparison_result` field changes on rollback.

### Verification that the rollback is clean

```bash
# All ten new threshold tests are expected to disappear with the revert.
pytest tests/promotion/test_grounding_gate.py -q
# Existing gate behaviour must remain green:
pytest tests/promotion/ tests/grounding/ tests/comparison/ -q
```

`verification_command`: `pytest tests/promotion/ tests/grounding/ tests/comparison/`

### Cross-PR dependency

`depends_on`: #192 (Phase 1 verbatim grounding gate — this PR extends
the gate's existing reject-paths without altering their semantics).
The threshold runs BEFORE the byte-match check; an item that fails the
threshold is rejected as `grounding_source_quote_too_short` instead of
falling through to the byte-match (the reason code surfaces the
precision problem at its actual cause).

`future_dependency`: a follow-up may flip the threshold to ON by
default in `grounding_gated_payload` and/or `verify_grounding`, OR
plumb a CLI flag through `spectrum-core meeting-minutes-llm` and
`scripts/compare_opus_haiku.py` to opt in at run time. Either
extension requires a fresh rollback contract entry because either
would change F1 measurement against the existing data-lake baseline.

### Operator action after merge

None required. The threshold ships OFF; F1 measurement is unchanged.
To experiment with the Stage 2 precision lever, a caller passes
`min_quote_chars_by_type=DEFAULT_MIN_QUOTE_CHARS_BY_TYPE` to
`verify_grounding` or `grounding_gated_payload` directly from a
research script (no CLI surface is added by this PR).

---

## Phase 2.B — chunk overlap (PR #217)

### What this change adds

- New `CHUNK_OVERLAP_TURNS` environment variable (int, default `0`).
  Read by both speaker-turn chunkers:
  `src/spectrum_systems_core/extraction/chunker.py` (cascade pipeline)
  and `src/spectrum_systems_core/data_lake/chunker.py` (live-LLM
  extraction). When `> 0`, each speaker-turn chunk at position `i`
  has its `text` field prepended with the text of the prior
  `min(N, i)` turns; the recipient chunk records the prepended turn
  IDs and the prepended count. Hard ceiling: if prepending would
  push the chunk past `MAX_CHUNK_CHARS` (cascade) or the new
  `MAX_LLM_CHUNK_CHARS` constant (live-LLM), the overlap count is
  reduced to 1, then 0, and `overlap_clamped: true` is recorded.
  Clamping never raises — it logs a warning.
- Three optional per-chunk metadata fields stamped onto both chunker
  outputs (absent when `CHUNK_OVERLAP_TURNS=0` — pre-Phase-2.B
  byte-identicality preserved):
  - `overlap_turns_prepended: int` — count of overlap turns
    prepended to this chunk (0 when no overlap applies, e.g. the
    first chunk).
  - `overlap_clamped: bool` — true when the hard ceiling forced the
    overlap count below `CHUNK_OVERLAP_TURNS`.
  - `prepended_overlap_turn_ids: list[str]` — turn_ids (or
    chunk_ids for the cascade) of the prepended turns, retained
    verbatim from the source chunks (no new IDs minted).
- New optional `chunking_strategy_version: str` field on the
  `meeting_minutes` artifact's `provenance` block. Stamped at
  extraction time by `workflows/meeting_minutes_llm.py` (and the
  Sonnet / Opus / cascade variants). Values:
  - `"speaker_turn_v1"` — current behaviour, zero overlap. Treated
    as the default for any pre-Phase-2.B artifact that does NOT
    carry the field.
  - `"speaker_turn_v1_overlap{N}"` — overlap of `N` turns.
- New gate function `verify_no_overlap_only_attribution` in
  `src/spectrum_systems_core/promotion/gate.py`. Accepts the
  artifact and an explicit `overlap_only_turn_ids: set[str]`
  parameter (computed by the caller from the chunk envelope).
  Emits reason code `failed:extracted_from_overlap_context` when
  an extracted item's `source_turn_ids` reference ONLY overlap-
  tagged turn IDs. Mixed (overlap + non-overlap) and pure
  non-overlap items pass through.
- New gate in `scripts/compare_opus_haiku.py` that halts with
  reason code `chunking_strategy_mismatch` when the two artifacts
  being compared declare different `chunking_strategy_version`
  values (with absent/None treated as `"speaker_turn_v1"`). The
  halt is fail-closed; an operator wishing to compare across
  strategies for measurement-only runs must produce a matched
  baseline by re-running the reference model under the same
  `CHUNK_OVERLAP_TURNS` setting.
- Strategy-aware haiku artifact selection in
  `scripts/compare_opus_haiku.py` (PR #220) — when multiple haiku
  artifacts at different `chunking_strategy_version` values exist
  in the same meeting directory, the selector filters candidates
  to those matching the Opus baseline's strategy (or the new
  `--chunking-strategy <version>` CLI override, surfaced as a
  `chunking_strategy` workflow input on
  `.github/workflows/run-comparison.yml`) BEFORE recency / content
  ordering, halting `no_haiku_artifact_matching_strategy` rather
  than silently picking a wrong-strategy artifact. The override
  controls SELECTION only; the `chunking_strategy_mismatch` halt
  above still fires if the resulting haiku artifact's strategy
  differs from the baseline's, so the flag is not a gate bypass.
  Default behaviour (auto-detect from the Opus baseline) is
  unchanged when only one haiku artifact exists.
- Two new optional aggregate fields on
  `chunk_merge_summary.json`/`chunk_split_summary.json`:
  `overlap_turns_prepended_total: int`,
  `overlap_clamped_count: int`.
- New workflow `.github/workflows/run-haiku-overlap-extraction.yml`
  mirroring `run-haiku-extraction.yml` but passing
  `CHUNK_OVERLAP_TURNS=2`. Phone-safe operator-driven dispatch.
- `.github/workflows/create-opus-reference-baselines.yml` — added
  `chunk_overlap_turns` input (PR #218). The new input is passed
  through to `scripts/create_opus_reference_baselines.py` as the
  `CHUNK_OVERLAP_TURNS` env var; the script reads it via the shared
  `chunking_strategy_version()` helper (the SSOT shipped in this
  Phase 2.B PR) and stamps the resulting token on every
  `opus_reference_minutes.jsonl` row so the comparison engine's
  per-row `_chunking_strategy_version_of()` lookup finds a matched
  value against an overlap=N haiku artifact. The same PR sets the
  `source_id` input default to the Dec 18 transcript slug so the
  workflow can be dispatched from a phone with a single tap. Default
  `chunk_overlap_turns=0` yields `speaker_turn_v1` (no suffix) —
  byte-compatible with pre-Phase-2.B baselines on disk that omit the
  field (the comparator defaults a missing value to the same string).
- New tests:
  - `tests/extraction/test_overlap_attribution_gate.py` — pins the
    overlap-attribution gate behaviour (rejection, mixed-pass,
    happy-path, and CHUNK_OVERLAP_TURNS=0 no-op).
  - `tests/comparison/test_chunking_strategy_version_gate.py` —
    pins the comparison-engine halt behaviour (mismatch reject,
    matched pass, both-null pass, one-null-one-explicit pass).
  - Additions to `tests/data_lake/test_chunker.py` and
    `tests/extraction/test_chunker.py` covering the overlap path,
    the hard-ceiling clamp, and the byte-identical default.

### To roll back

1. Revert this PR. The two chunkers stop reading
   `CHUNK_OVERLAP_TURNS`; the overlap metadata fields are no longer
   stamped onto new chunks; the `chunking_strategy_version` field
   is no longer stamped onto new `meeting_minutes` artifacts.
2. Existing artifacts in the data lake that carry
   `chunking_strategy_version` remain readable. The reverted
   schema retains the field as optional (it is documented additive
   and `additionalProperties` on `provenance` is intentionally not
   strict per the meeting_minutes.schema.json comment at line
   1540). The comparison engine post-revert treats any stamped
   value as `"speaker_turn_v1"` and proceeds without halting.
3. Existing chunks on disk in `chunks.jsonl` files that carry the
   new metadata fields (`overlap_turns_prepended`,
   `overlap_clamped`, `prepended_overlap_turn_ids`) remain readable
   under the reverted code because the downstream consumers either
   read by-key with `.get()` defaults or ignore unknown keys. The
   reverted chunk schema continues to allow optional fields.
4. The two gate functions
   (`verify_no_overlap_only_attribution`, the comparison-engine
   strategy-mismatch halt) disappear with the revert. Existing
   `grounding_rejection_report` or `comparison_failure` artifacts
   that carry the new reason codes remain on disk as inert
   diagnostics.
5. The `.github/workflows/run-haiku-overlap-extraction.yml`
   workflow disappears; any operator who dispatched it before the
   revert keeps the resulting artifacts (with the overlap
   provenance stamp) — they are valid for read but not for
   forward comparison under matched baselines.

### Data migration required for rollback

None. All Phase 2.B additions are additive at every layer:

- `CHUNK_OVERLAP_TURNS` defaults to `0`; the default-off path is
  byte-identical to pre-Phase-2.B output.
- `chunking_strategy_version` is optional on `provenance`;
  artifacts without it validate against both the pre- and
  post-Phase-2.B schema.
- `prepended_overlap_turn_ids`, `overlap_turns_prepended`,
  `overlap_clamped` are optional on the chunk envelope and on the
  merge/split summaries.
- The gate functions are net-new; reverting deletes them, and no
  pre-Phase-2.B caller invokes them.

### Verification that the rollback is clean

```bash
pytest tests/extraction/test_overlap_attribution_gate.py
pytest tests/comparison/test_chunking_strategy_version_gate.py
pytest tests/data_lake/test_chunker.py
pytest tests/extraction/test_chunker.py
python scripts/verify_rollback_contracts.py --pr 217
```

After revert, the first two test files are expected to disappear.
If either remains and fails, the revert is incomplete — fix
forward. The chunker test suites remain present and are expected
to PASS post-revert because they pin behaviour under the default
`CHUNK_OVERLAP_TURNS=0`, which the revert restores byte-identically.

`verification_command`: `pytest tests/extraction/test_overlap_attribution_gate.py tests/comparison/test_chunking_strategy_version_gate.py tests/data_lake/test_chunker.py tests/extraction/test_chunker.py`

### Cross-PR dependency

`depends_on`: #192 (Phase 1 verbatim grounding gate — this PR adds
a sibling check `verify_no_overlap_only_attribution` in the same
module, reusing the `RejectionRecord`/`AcceptanceRecord` types and
the per-item `source_turn_ids` shape).

`future_dependency`: a follow-up may flip `CHUNK_OVERLAP_TURNS`
default from `0` to a measured value, or wire it through the CLI
as an explicit `--chunk-overlap-turns` flag. Either extension
requires a fresh rollback contract entry because either would
change F1 measurement against the existing data-lake baseline.

### Operator action after merge

The default path (`CHUNK_OVERLAP_TURNS` unset / 0) is byte-identical
to pre-Phase-2.B. To measure the overlap effect:

1. Re-run the Opus reference baseline under
   `CHUNK_OVERLAP_TURNS=2` (matched baseline).
2. Dispatch
   `.github/workflows/run-haiku-overlap-extraction.yml` on the
   Dec 18 transcript.
3. Compare the new Haiku artifact against the matched Opus
   baseline via `scripts/compare_opus_haiku.py` (the strategy-
   version gate will pass because both artifacts now carry
   `"speaker_turn_v1_overlap2"`).
4. The PR hard gate is F1 ≥ 39.5% (no regression vs prior
   baseline of 39.5%).

---

## Phase 2.C — cascade filter first dispatch (PR #221)

### What this change adds

- `docs/runbooks/phase_2c_cascade_rollback.md` — operator-facing
  runbook for the first dispatch of the Stage 2 cascade filter on a
  real transcript. Documents revert, gate-bug response (drop-rate
  collapse), and artifact handling.
- `tests/cascade/fixtures/phase_2c_smoke_items.json` (PR-2) — a static
  3-item fixture derived from the existing Haiku artifact
  `d019c5f793c4` (the Dec 18 7 GHz Downlink TIG kickoff). The fixture
  carries one well-grounded `procedural_ruling` item, one vague
  `action_items` item, and one synthetic `action_items` item with
  `source_quote: null`. The fixture is fully static so CI does not
  read the data-lake.
- `tests/cascade/test_cascade_smoke_real_items.py` (PR-2) — a smoke
  test that drives the production `run_cascade_filter` (from
  `src/spectrum_systems_core/cascade/executor.py`) against the
  fixture. The test pins three behaviours: keep on the well-grounded
  item, drop on the vague item, and graceful pass-through (no
  exception, decision applied) on the null-`source_quote` item.
- NO changes to `src/spectrum_systems_core/cascade/` (already shipped
  in PR #203).
- NO changes to `src/spectrum_systems_core/schemas/`.
- NO new artifact types.
- NO changes to `.github/workflows/run-comparison.yml` (the
  `use_cascade_output` input already exists per the Phase 6 entry
  above).

The post-merge operator dispatch (`run-comparison.yml` with
`use_cascade_output=true` on the Dec 18 transcript) is documented
separately in the runbook above. The dispatch produces three
append-only on-disk artifacts in the data-lake
(`meeting_minutes_filtered__*.json`, `cascade_filter_log__*.json`, and
a `comparison_result` row stamped
`prompt_variant=production_haiku_with_cascade_filter`); none are
promoted product artifacts; none enter
`indexes/meetings/artifact_index.jsonl`.

### To roll back

1. Revert PR-1 (this docs entry + the runbook). Docs-only revert; no
   on-disk artifact is affected. The companion runbook in
   `docs/runbooks/phase_2c_cascade_rollback.md` disappears.
2. Revert PR-2 (the smoke test + fixture). The test and fixture
   disappear; no on-disk artifact is affected. The cascade module is
   unchanged, so the cascade behaviour is unchanged.
3. Stop dispatching the cascade. Subsequent
   `.github/workflows/run-comparison.yml` invocations leave
   `use_cascade_output=false` (the default), so the comparison
   returns to the raw Haiku artifact path. The
   `--enable-cascade-filter` CLI flag remains opt-in (default OFF) per
   the Phase 6 entry above.
4. Existing cascade-filtered artifacts in the data-lake remain on
   disk. The data-lake is append-only from core's perspective per
   `docs/contracts/data_lake_contract.md` §8; do NOT delete them. If
   the cascade output is found to be producing bad output post-run
   (recall collapse), mark them `superseded: true` per the runbook's
   gate-bug response section (§3). Comparisons performed AFTER a
   superseded marker is set MUST skip the superseded artifact and
   fall back to the base Haiku artifact.

### Data migration required for rollback

None. The cascade was already in place per PR #203; Phase 2.C only
turns it on and adds a smoke test. Reverting Phase 2.C is opt-in by
construction — the cascade defaults to OFF on every CLI invocation,
and the workflow input defaults to `use_cascade_output=false`.

### Verification that the rollback is clean

```bash
python scripts/verify_rollback_contracts.py --pr <PR-1-number>
pytest tests/cascade/test_cascade_smoke_real_items.py
```

`verification_command`: `pytest tests/cascade/test_cascade_smoke_real_items.py`

If `verify_rollback_contracts.py` fails, the entry is incomplete — fix
forward (the script reads this file and asserts the entry references
the PR's changed files plus a whitelisted verification command). If
the smoke test fails after PR-2 has landed, do NOT proceed with the
operator dispatch step in the runbook — investigate the cascade
behaviour first.

### Cross-PR dependency

`depends_on`: #203 (Phase 6 — the cascade filter module itself).
Phase 2.C cannot land without Phase 6 because the cascade dispatch
path lives in PR #203.

`also_depends_on`: #220 (Phase 2.B strategy-aware haiku selection),
because the post-merge dispatch step relies on the
`chunking_strategy` selector to pick the correct Haiku artifact for
the cascade input. The base Haiku artifact at `d019c5f793c4` was
produced under `speaker_turn_v1`; PR #220 ensures the selector
matches that strategy.

### Operator action after merge

1. Dispatch `.github/workflows/run-comparison.yml` with:
   - `use_cascade_output=true`
   - `source_id=7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`
   - `chunking_strategy` blank (auto-detect from the Opus baseline)
2. Read the step summary. The required fields are: cascade F1 vs
   Opus, cascade drop rate, Opus baseline artifact path, token cost
   (if the cost estimator is wired in).
3. Hard gate: F1 ≥ 39.5% (no regression vs the `speaker_turn_v1`
   Haiku baseline of 39.5%).
   - F1 < 39.5%: halt. Apply the gate-bug response in the runbook
     §3 (likely the drop rate is too aggressive).
   - 39.5% ≤ F1 < 45%: cascade is working but not at target.
     Proceed to Phase 2.D.
   - F1 ≥ 45%: Phase 2.C closes. Proceed to Phase AC (corpus
     comparison across the 13 transcripts).

---

## Phase 2.C schema fixes — add `clarification` to `position_type` enum (PR #TBD)

### What this change adds

- Additive enum extension on
  `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`:
  `position_statement.position_type` gains the value `clarification`.
  Before: `["support", "opposition", "conditional", "neutral",
  "unclear"]`. After: `["support", "opposition", "conditional",
  "neutral", "unclear", "clarification"]`. Pre-existing artifacts
  whose `position_type` is any of the original five values validate
  unchanged. No other field on `position_statement` is touched and no
  other item type is modified.
- No `schema_version` bump. Same precedent as the attendees.agency
  null fix (commit `d2e23d7`) and the Phase 6 cascade prompt-variant
  additive enum extension above: backward-compatible additive value
  expansion keeps the const version. The `meeting_minutes.schema.json`
  `schema_version` enum (`1.0.0` … `1.4.0`) is unchanged. The schema-
  level additivity rule is documented inline on the `schema_version`
  property at line 29 of the schema.
- Prompt updates to keep producers in sync with the schema:
  `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  (the enum line on the strict-schema callout) and
  `src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md`
  (the natural-language enum sentence and the JSON skeleton). No
  prompt logic is rewritten; only the listed allowed values change.
- `docs/architecture/artifact_manifest.md` `position_statement` entry
  updated to reflect the new enum.
- New tests in `tests/test_meeting_minutes_schema.py`:
  - `test_position_statement_each_type_validates` now parametrizes
    over all six values (the original five plus `clarification`).
  - `test_position_statement_clarification_validates` — explicit
    happy-path assertion that an artifact carrying
    `position_type: "clarification"` validates.
  - `test_position_statement_invalid_value_fails` — explicit
    rejection assertion that `position_type: "invalid_value"`
    fails-closed. The pre-existing
    `test_position_statement_position_type_outside_enum_fails`
    (rejection with `"maybe"`) is preserved unchanged.
- NO changes to the agency-level
  `contracts/schemas/agency/position_entry.schema.json` enum
  (`supports`, `opposes`, `conditionally_supports`,
  `requests_clarification`, `raises_concern`). That is a different
  schema for a different module (`agency/profile_builder.py`) and
  uses a different taxonomy. Out of scope for this fix.
- NO new gate, no new eval, no new artifact type.

### Domain candidates noted but NOT added

Two candidate values were considered per the task brief
(`abstention`, `deferral`). Neither was added because there is no
evidence of either appearing as a `position_type` value in existing
extraction fixtures, prompts, or governed-loop fixtures (a repo-wide
grep finds `deferral` only as a `decision_outcome` enum value in
`src/spectrum_systems_core/config/taxonomy.py` and the decision
extractor — a different field, different schema). If a future
transcript surfaces either value as `position_type`, the same
additive-extension pattern in this entry applies.

### To roll back

1. Revert the PR. The five-value enum returns to
   `meeting_minutes.schema.json`. Prompts and the artifact manifest
   revert in lockstep.
2. The two new tests
   (`test_position_statement_clarification_validates`,
   `test_position_statement_invalid_value_fails`) and the extra
   parametrize value (`clarification`) disappear.
3. Existing artifacts in the data-lake that carry
   `position_type: "clarification"` (the Dec 18 cascade run that
   motivated this PR is the first known producer) become invalid
   against the reverted schema and would be blocked by the strict-
   schema eval on re-validation. The data-lake is append-only per
   `docs/contracts/data_lake_contract.md` §8; do NOT delete them.
   If revert is required, re-extract the affected source(s) so a
   non-`clarification` value is produced, or accept that the
   superseded artifact is no longer re-validateable.

### Data migration required for rollback

None for the schema or codebase. Any `clarification`-bearing artifact
on disk falls out of strict-schema validity until either re-extracted
or until the enum is re-extended in a follow-up PR.

### Verification that the rollback is clean

```bash
pytest tests/test_meeting_minutes_schema.py
```

`verification_command`: `pytest tests/test_meeting_minutes_schema.py`

### Cross-PR dependency

`depends_on`: PR #182 (the attendees.agency null fix — same fix
pattern: a semantically valid value emitted by Haiku is widened in
the schema fail-closed rather than retried) and PR #205 (event_id
null — same pattern). No code dependency, only the precedent that
backward-compatible widening of producer-facing constraints is the
durable fix vs gate retries.

`no_future_dependency`: this entry does not gate any future PR.

### Operator action after merge

1. Re-dispatch `.github/workflows/run-cascade-filter.yml` (or the
   equivalent extraction workflow per the runbook) on the Dec 18
   transcript (`source_id=7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`).
   The `position_type: "clarification"` item that previously blocked
   the cascade now passes the strict-schema eval.
2. Dispatch `.github/workflows/run-comparison.yml` with
   `use_cascade_output=true` and the same `source_id`. Confirm the
   comparison row is produced (the same F1 / drop-rate gate from the
   Phase 2.C entry above applies).

---

## Opus baseline schema_version canonical source (PR #231)

### What this change adds

- New public constant
  `GROUNDING_BINDING_SCHEMA_VERSION = "1.4.0"` on
  `src/spectrum_systems_core/promotion/gate.py`, re-exported from
  `src/spectrum_systems_core/promotion/__init__.py`. The constant is
  the SINGLE canonical source for the active `meeting_minutes`
  schema_version any new producer should stamp. No new gate, no new
  schema version (1.4.0 was already binding from Phase 1, PR #128 /
  commit `b14b473`); this PR consolidates the value behind one
  importable name so future bumps are a one-line edit.
- `scripts/create_opus_reference_baselines.py` reads
  `schema_version` for every JSONL row from the canonical constant
  (was hard-coded `"1.0.0"` at line 859). This is the bug fix that
  motivated the PR — the `schema_version_mixed` halt fired on every
  Haiku-vs-Opus comparison because the baseline writer was missed
  during the Phase 1 schema bump.
- `scripts/compare_opus_haiku.py` reads
  `_GROUNDING_BINDING_SCHEMA_VERSION` (the comparator's
  binding-version threshold) and the cascade synthetic envelope's
  `schema_version` from the same canonical constant. Both were
  previously string literals.
- `tests/test_create_opus_reference_baselines.py` gains a new
  regression test
  `test_opus_baseline_schema_version_matches_canonical_source_no_string_literal`
  asserting every baseline row stamps the canonical value and that
  the canonical value has not silently reverted to legacy `"1.0.0"`.
  The pre-existing `test_valid_transcript_writes_correct_fields`
  schema_version assertion is migrated to read from the canonical
  constant instead of a string literal.
- NO new artifact type. NO new schema version. NO new CLI flag. NO
  new gate. The promotion/gate module's public surface gains exactly
  one constant, exported from `promotion/__init__.py`.

### To roll back

1. Revert the PR. The constant disappears from
   `src/spectrum_systems_core/promotion/gate.py` and from
   `src/spectrum_systems_core/promotion/__init__.py`. The two
   scripts and the test re-acquire the string literals they had
   before — `"1.0.0"` in the Opus baseline writer (line 859) and
   `"1.4.0"` in the comparator's binding constant + cascade
   synthetic envelope.
2. Existing `opus_reference_minutes.jsonl` files written under this
   PR carry `schema_version: "1.4.0"` per the fix. The data lake is
   append-only (`docs/contracts/data_lake_contract.md` §8); do NOT
   delete or rewrite them. After revert, the comparator's
   `_baseline_at_version_exists("1.4.0")` check will still match
   these files because they continue to declare `1.4.0` — the
   comparator reads from disk, not from the constant.
3. Pre-PR baselines on disk that carry `schema_version: "1.0.0"`
   remain valid against the reverted writer; they will, however,
   re-trigger the `schema_version_mixed` halt against any 1.4.0+
   Haiku artifact. Operators who want the comparison to succeed
   either re-baseline (post-revert the writer still hard-codes
   `1.0.0` so this does not help) or pass the existing CLI-only
   `--allow-mixed-schema` override on `compare_opus_haiku.py`. In
   practice the rollback path is "fix forward" rather than revert.

### Data migration required for rollback

None at the schema or codebase layer. Any baseline JSONL written
under this PR keeps its `schema_version: "1.4.0"` rows on disk and
remains comparable against a 1.4.0 Haiku artifact after the revert
because the comparator's coherence check reads the row's stamped
value, not the (now-removed) constant.

### Verification that the rollback is clean

```bash
pytest tests/test_create_opus_reference_baselines.py
```

After revert the new regression test (and the canonical-constant
import in the migrated existing test) ceases to exist with the
file; the surviving tests must still all pass. If
`tests/test_create_opus_reference_baselines.py` fails after revert,
the rollback was not complete — fix forward.

`verification_command`: `pytest tests/test_create_opus_reference_baselines.py`

### Cross-PR dependency

`depends_on`: PR #128 (commit `b14b473`, Phase 1 verbatim span
grounding gate + 1.4.0 schema bump). This PR closes a loose end
from that schema bump: the Opus baseline writer was missed and
silently stayed at `"1.0.0"`, producing `schema_version_mixed` on
every comparison until this fix.

`no_future_dependency`: this entry does not gate any future PR.

### Operator action after merge

1. Re-dispatch `.github/workflows/run-opus-baseline.yml` (or
   equivalent) on every `source_id` whose
   `opus_reference_minutes.jsonl` still carries
   `schema_version: "1.0.0"`. The new baseline rows will stamp
   `"1.4.0"` and the comparator's coherence check will pass.
2. Re-dispatch `.github/workflows/run-comparison.yml` for the
   re-baselined sources. The previously-blocked
   `schema_version_mixed` runs will now succeed.

---

## Phase 3.A — G-PROMPT-NEGATIVE + G-REASON-FIELD (PR #235)

### What this change adds

- `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`:
  optional `reason` field (string, `minLength: 5`, `maxLength: 500`)
  added to 12 claim-shaped types — `open_questions`, `commitments`,
  `claims`, `risks`, `cross_references`, `regulatory_references`,
  `issue_registry_entry`, `position_statement`, `dissent_or_objection`,
  `precedent_reference`, `external_stakeholder_input`,
  `procedural_ruling`. The pre-existing Phase 3P `reason` field on
  `decisions` and `action_items` is tightened from `minLength: 1`
  (no max) to the same `minLength: 5, maxLength: 500` for parity
  with the other 13 reason-bearing types. NO new artifact type. NO
  new schema version (additive optional fields per the
  `schema_version` enum policy documented at
  `meeting_minutes.schema.json` line 29; the canonical
  `promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION` constant remains
  `"1.4.0"`).
- `src/spectrum_systems_core/schemas/meeting_extraction.schema.json`:
  same optional `reason` field added to the three parallel types
  (`decisions`, `claims`, `action_items`). Each items schema declares
  `additionalProperties: false`, so the field had to be admitted at
  the schema layer before the prompt could emit it; the schema
  predicates have not changed otherwise.
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  and `meeting_minutes_opus.md`: new `## DO NOT EXTRACT` section (8
  non-extractable categories — brainstorming, prior-decision recaps,
  agenda restatements, conditional/speculative statements,
  meta-procedural talk, third-party quotes, repeated mentions, bare
  numeric mentions) and new `## Reason field (REQUIRED on 14
  claim-shaped types)` section carrying the forcing-function
  sentence "If you cannot articulate a reason in one sentence, DO
  NOT extract this item." Both prompts updated in lockstep — the
  `DO NOT EXTRACT` section is byte-identical between the two so the
  Haiku/Opus F1 comparison stays interpretable.
- `tests/test_prompt_negative_and_reason_field.py`: new pinning
  test file. 27 tests covering byte-identity of `DO NOT EXTRACT`
  across both prompts, forcing-function sentence presence,
  per-type `reason` schema presence on the 14 claim-shaped types,
  per-type absence on the 9 descriptive types
  (`attendees`, `agenda_item`, `meeting_phases`, `topics`,
  `scheduled_events`, `technical_parameters`, `named_artifacts`,
  `sentiment_indicators`, `glossary_definition`), and the
  canonical-constant invariant.
- NO new gate. NO new CLI flag. NO new artifact type. NO new
  workflow. The change is prompt-side (precision-at-extraction) +
  additive schema fields that match what the prompt now requires.

### To roll back

1. Revert the PR. The 12 newly-added `reason` properties disappear
   from `meeting_minutes.schema.json`, the 3 from
   `meeting_extraction.schema.json`. The Phase 3P `reason` field on
   `decisions` / `action_items` reverts to its pre-3.A form
   (`minLength: 1`, no `maxLength`). The `## DO NOT EXTRACT` and
   `## Reason field (REQUIRED on 14 claim-shaped types)` sections
   disappear from both prompt files. The new test file is removed
   with the revert.
2. Existing meeting_minutes artifacts on the data lake that were
   produced under Phase 3.A and carry a `reason` field on one of
   the 12 newly-permitted types will FAIL strict-schema validation
   against the reverted schema, because
   `additionalProperties: false` on each items schema rejects the
   unknown field. The data lake is append-only
   (`docs/contracts/data_lake_contract.md` §8); do NOT delete or
   rewrite them. Operators have three rollback options, in
   preference order:
   - Fix forward: re-apply the additive `reason` field on the
     affected types. This restores forward compatibility without
     touching data.
   - Re-baseline the affected sources by re-dispatching the
     production extraction workflow under the reverted (pre-3.A)
     prompt. The new artifacts will omit `reason` and validate
     cleanly.
   - Tolerate the validation failure only on sources you
     deliberately want to retire; promote nothing from them under
     the reverted schema.
3. Existing artifacts produced BEFORE this PR (no `reason` on any
   of the 14 types) validate unchanged against the reverted schema
   — the field is optional everywhere.

### Data migration required for rollback

None for pre-3.A artifacts (the schema change is additive optional;
they continue to validate). Post-3.A artifacts carrying `reason` on
one of the 12 newly-permitted types require re-baselining if a
strict revert is taken; "fix forward" by re-adding the additive
field is the recommended path and requires no data migration.

### Verification that the rollback is clean

```bash
pytest tests/test_prompt_negative_and_reason_field.py
```

After revert this file ceases to exist with the rollback; the
verification is that the rest of the test suite still passes (no
production code path depended on the field existing). If any other
test fails after revert, the rollback was not complete — fix
forward.

`verification_command`: `pytest tests/test_prompt_negative_and_reason_field.py`

### Cross-PR dependency

`depends_on`: PR #198 (Phase 3P `reason` field on
`decisions`/`action_items`). PR #235 generalises Phase 3P's
single-type pattern to 14 types and tightens the length bound for
parity. Reverting PR #198 underneath PR #235 would leave the
schema referencing a non-existent prior pattern; if both are ever
reverted, revert PR #235 first.

`no_future_dependency`: this entry does not gate any future PR.
Future phases (3.B, 4.x, etc.) may extend or relax the reason
constraints without rolling this PR back.

### Operator action after merge

1. The next baseline extraction run will emit `reason` on every
   item of the 14 claim-shaped types. The comparison engine's
   text-field resolver already handles unknown extra fields via
   dict iteration; no operator action required to read the new
   field downstream.
2. Re-dispatch `.github/workflows/compare-opus-haiku.yml` on the
   representative source set to measure Phase 3.A F1 against the
   39.5% gate. That measurement run is OUT OF SCOPE for this PR;
   the merge does not depend on it.

---

## Phase 3.B–E — taxonomy, modal policy, glossary, few-shot (PR #236)

### What this change adds

- `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`:
  optional `decision_subtype` enum (`issue` | `proposal` |
  `resolution` | `scope`) added to the structured-object branch of
  `decisions`. The Phase 3.B Fernández (SIGDIAL 2008) implicit-
  decision sub-type. Schema-additive only — the field is OPTIONAL,
  legacy artifacts that omit it validate unchanged. NO new artifact
  type. NO schema_version bump (canonical
  `promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION` constant
  remains `"1.4.0"`; additive optional fields do not require it per
  the schema's `schema_version` enum policy).
- `src/spectrum_systems_core/schemas/meeting_extraction.schema.json`:
  same optional `decision_subtype` enum on the `decisions` items
  schema (which declares `additionalProperties: false`, so the
  field had to be admitted at the schema layer before the prompt
  could emit it). Schema predicates have not changed otherwise.
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  and `meeting_minutes_opus.md`: four new sections added in
  byte-identical lockstep wrapped in `<!-- *_BEGIN -->` /
  `<!-- *_END -->` markers — `## NTIA/DoD SPECTRUM GLOSSARY`
  (38 NTIA/DoD terms, placed at top before DO NOT EXTRACT),
  `## IMPLICIT DECISION RECOGNITION (Fernández et al., SIGDIAL
  2008)` (four-subtype taxonomy with explicit linguistic markers
  and decision_subtype routing), `## MODAL VERB POLICY` (per
  NTIA Manual Chapter 5: shall/will/should/may/could/would/might
  routing rules), and `## FEW-SHOT EXAMPLES` (three hand-curated
  examples in recency-bias order: explicit → near-miss →
  implicit). Prompt version bumped 3.A → 3.B-E. The Opus prompt's
  "Why this prompt is different from the Haiku extraction prompt"
  section was rewritten to reflect the new lockstep alignment.
- `tests/test_phase_3b_3e_prompt_additions.py`: new pinning test
  file. 8 tests covering taxonomy section presence (both prompts),
  taxonomy markers (≥5 canonical markers in each, anti-regression
  on `what if we`), modal policy section presence with
  shall/will/should/may classification rules, glossary section
  presence (≥30 canonical terms in each), byte-identical glossary
  block across prompts, three few-shot examples in correct order
  (explicit → near-miss → implicit), byte-identical few-shot block
  across prompts, no regression on the Phase 3.A DO NOT EXTRACT
  section, and prompt versions higher than 3.A.
- NO new gate. NO new CLI flag. NO new artifact type. NO new
  workflow. NO change to cascade, chunking, comparison, or any
  pipeline infrastructure. The change is prompt-side
  (precision-at-extraction-time) + an additive optional schema
  enum that matches what the prompt now offers.

### To roll back

1. Revert the PR. The `decision_subtype` property disappears from
   both `meeting_minutes.schema.json` and
   `meeting_extraction.schema.json`. The four new prompt sections
   (`NTIA/DoD SPECTRUM GLOSSARY`, `IMPLICIT DECISION RECOGNITION`,
   `MODAL VERB POLICY`, `FEW-SHOT EXAMPLES`) disappear from both
   prompt files. The Opus prompt's "Why this prompt is different
   from the Haiku extraction prompt" section reverts to its
   pre-3.B-E text. The Haiku prompt's pre-existing Phase 1.4
   implicit-decision taxonomy block and Phase 3P few-shot block
   are NOT touched by this PR and remain in place after revert.
   The new test file is removed with the revert.
2. Existing meeting_minutes artifacts on the data lake that were
   produced under Phase 3.B-E and carry `decision_subtype` on a
   `decisions` item will FAIL strict-schema validation against the
   reverted schema, because `additionalProperties: false` on the
   decisions items schema rejects the unknown field. The data lake
   is append-only (`docs/contracts/data_lake_contract.md` §8); do
   NOT delete or rewrite them. Operators have three rollback
   options, in preference order:
   - Fix forward: re-apply the additive `decision_subtype` enum on
     `decisions`. This restores forward compatibility without
     touching data.
   - Re-baseline the affected sources by re-dispatching the
     production extraction workflow under the reverted (pre-3.B-E)
     prompt. The new artifacts will omit `decision_subtype` and
     validate cleanly.
   - Tolerate the validation failure only on sources you
     deliberately want to retire; promote nothing from them under
     the reverted schema.
3. Existing artifacts produced BEFORE this PR (no
   `decision_subtype` on any `decisions` item) validate unchanged
   against the reverted schema — the field is optional.

### Data migration required for rollback

None for pre-3.B-E artifacts (the schema change is additive
optional; they continue to validate). Post-3.B-E artifacts
carrying `decision_subtype` on `decisions` require re-baselining
if a strict revert is taken; "fix forward" by re-adding the
additive enum is the recommended path and requires no data
migration.

### Verification that the rollback is clean

```bash
pytest tests/test_phase_3b_3e_prompt_additions.py
```

After revert this file ceases to exist with the rollback; the
verification is that the rest of the test suite still passes (no
production code path depended on the field existing or on the
new prompt sections). If `tests/test_prompt_negative_and_reason_field.py`
(Phase 3.A regression tests) and
`scripts/verify_trigger_taxonomy.py` (Phase 1.4 baseline) both
still pass after revert, the rollback is clean.

`verification_command`: `pytest tests/test_phase_3b_3e_prompt_additions.py`

### Cross-PR dependency

`depends_on`: PR #235 (Phase 3.A `DO NOT EXTRACT` + `reason`
field). PR #236's `FEW-SHOT EXAMPLES` Example 2 references the
Phase 3.A `DO NOT EXTRACT` brainstorming category by name, and
the new sections are placed AFTER the Phase 3.A DO NOT EXTRACT /
Reason field blocks. Reverting PR #235 underneath PR #236 would
leave dangling references in the few-shot rationale. If both are
ever reverted, revert PR #236 first.

`no_future_dependency`: this entry does not gate any future PR.
Future phases may extend or replace the taxonomy, modal policy,
glossary, or few-shot sections without rolling this PR back.

### Operator action after merge

1. The next baseline extraction run will optionally emit
   `decision_subtype` on `decisions` object items when the prompt
   classifier maps the implicit-decision sub-type. The comparison
   engine's field resolver already handles unknown extra fields
   via dict iteration; no operator action required to read the
   new field downstream.
2. Re-dispatch `.github/workflows/compare-opus-haiku.yml` on the
   representative source set to measure Phase 3.B-E F1 against
   the 39.5% gate. That measurement run is OUT OF SCOPE for this
   PR; the merge does not depend on it.
3. Human resolution of the `scope` vs `agreement` taxonomy
   conflict (this PR pinned `scope` to match the existing codebase
   enum in `scripts/verify_trigger_taxonomy.py`) is still open and
   not blocked by this PR.

---

## Phase 4.A — G-GROUND-VERBATIM source_quote gate (PR #237)

### What this change adds

- `src/spectrum_systems_core/promotion/grounding_gate.py`: new
  module implementing the Phase 4.A substring-based grounding
  gate. `normalize_for_grounding` applies smart-quote → straight
  mapping BEFORE NFKC (so `U+2033` DOUBLE PRIME is not mangled
  into `''` and lose the double-quote signal), then NFKC, then
  whitespace collapse; case preserved. `check_grounding` rejects
  every item in the 14 claim-shaped types whose `source_quote`
  is missing / empty / `< 10` chars after normalization / not a
  literal substring of its `source_chunk_id` chunk (or of the
  full transcript when `source_chunk_id` is absent — logged as a
  warning). The empty-quote branch is explicit and runs BEFORE
  the substring check because `"" in chunk_text` returns `True`
  in Python. `CLAIM_SHAPED_TYPES` frozenset pins the 14 types
  and is asserted in lockstep with the canonical iteration tuple
  inside `check_grounding`. Co-exists with the Phase 1
  `promotion/gate.py` offset-based gate; the two have separate
  schema-version constants (Phase 1: `GROUNDING_BINDING_SCHEMA_VERSION
  = "1.4.0"`, Phase 4.A: `GROUNDING_GATE_SCHEMA_VERSION = "1.5.0"`).
- `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`:
  `"1.5.0"` added to the `schema_version` enum and the
  description extended. Optional `source_chunk_id` field added to
  the structured-object branch of every claim-shaped item type
  (14 types). Optional `source_quote` field additively added to
  `open_questions` and `cross_references` (which previously only
  carried `source_turn_ids`); their `grounding_mode`
  discriminator stays `"turn_aggregate"` so the Phase 1 gate
  still uses `source_turn_ids` for those, while the Phase 4.A
  gate uses the new `source_quote` whenever a producer emits it.
  Schema-additive only — every new field is OPTIONAL; legacy
  artifacts validate unchanged. The canonical
  `promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION` constant is
  intentionally NOT bumped from `"1.4.0"` to `"1.5.0"` in this
  PR; the bump cascades into the 1.4.0 Opus baseline (PR #232)
  and into `scripts/compare_opus_haiku.py`'s
  `< _GROUNDING_BINDING_SCHEMA_VERSION` filters, and is deferred
  to a focused follow-up PR that handles the migration.
- `src/spectrum_systems_core/schemas/comparison_result.schema.json`:
  13 new optional fields added at the top-level properties —
  `pre_gate_haiku_count`, `pre_gate_haiku_f1`,
  `pre_gate_haiku_precision`, `pre_gate_haiku_recall`,
  `post_gate_haiku_count`, `post_gate_haiku_f1`,
  `post_gate_haiku_precision`, `post_gate_haiku_recall`,
  `grounded_count`, `ungrounded_count`, `gate_drop_rate`,
  `legacy_exempt_count`, `recall_collapse_warning`. Every field
  is OPTIONAL; pre-4.A comparison artifacts validate unchanged
  (PR #233's byte-equal invariant preserved — neither
  `compare_opus_haiku.py` nor `create_opus_reference_baselines.py`
  is modified by this PR).
- `scripts/run_grounding_gate.py`: new operator-facing script
  that locates the most recent `meeting_minutes__*.json` under
  `<data-lake>/store/processed/meetings/<source-id>/`, validates
  it via `scripts/_artifact_validator.validate_artifact`
  (CLAUDE.md integration co-requirement), runs the gate, and
  writes four artifacts. Carries the `--disable-grounding-gate`
  rollback flag.
- `.github/workflows/run-grounding-gate.yml`: phone-safe
  `workflow_dispatch` wrapper with `source_id` + `disable_gate`
  choice inputs. Emits a step summary with totals, top-5 failure
  reasons, and a recall-collapse warning when grounded rate is
  `< 50%`. Pushes the new artifacts to the data-lake with a
  commit message ending in the skip-ci marker (no internal
  spaces) so the data-lake repo does not re-trigger CI.
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  and `meeting_minutes_opus.md`: new
  `## VERBATIM SOURCE GROUNDING (REQUIRED)` section added
  between `## DO NOT EXTRACT` and `## Reason field`. Section text
  is BYTE-IDENTICAL between the two prompts (asserted by
  `test_verbatim_grounding_section_byte_identical`). Both prompt
  frontmatter `version` keys bumped to `4.A` and a new changelog
  entry appended; the existing 3.A–3.E changelog entries are
  preserved.
- Four new artifact types are written by the script (none are
  promoted product artifacts; all are audit / diagnostic):
  `grounded_items`, `ungrounded_items` (JSONL audit),
  `grounding_gate_result`, and `grounding_gate_bypass_record`.
  None enter `indexes/meetings/artifact_index.jsonl`.
- 61 new tests: `tests/test_grounding_gate.py` (35),
  `tests/test_grounding_prompt_section.py` (9),
  `tests/test_grounding_comparison_artifact.py` (9),
  `tests/integration/test_run_grounding_gate_script.py` (8).
  Plus a one-line update to `tests/test_meeting_minutes_schema.py`
  to include `"1.5.0"` in the canonical enum pin.

### To roll back

1. Revert the PR. The new module, script, workflow, prompt
   section, and 13 comparison-artifact fields disappear. The
   schema enum returns to `["1.0.0", "1.1.0", "1.2.0", "1.3.0",
   "1.4.0"]` and `source_chunk_id` disappears from all 14
   claim-shaped item-type schemas. The Phase 1
   `promotion/gate.py` is untouched by this PR and continues to
   enforce 1.4.0 verbatim-grounding semantics unchanged after
   revert. The Phase 3.B–E prompt sections (PR #236) and the
   Phase 3.A `reason` field (PR #235) are untouched by this PR
   and remain in place after revert.
2. Existing `meeting_minutes` artifacts that were produced at
   `schema_version == "1.5.0"` would FAIL strict-schema
   validation against the reverted schema because `"1.5.0"` is
   no longer in the enum. Per `docs/contracts/data_lake_contract.md`
   §8 the data lake is append-only; do NOT delete or rewrite
   them. Operators have two rollback options, in preference
   order:
   - Fix forward: re-apply the additive `"1.5.0"` enum value and
     the optional `source_chunk_id` field. This restores forward
     compatibility without touching data.
   - Re-baseline the affected sources by re-dispatching the
     production extraction workflow under the reverted (pre-4.A)
     prompt. The new artifacts will stamp `"1.4.0"` and validate.
3. Existing `comparison_result` artifacts carrying any of the 13
   new optional fields validate cleanly against the reverted
   schema (the comparison schema's root has `additionalProperties:
   false`, but every removed field is in `properties` — a revert
   that drops the field definition means an artifact carrying
   that field would fail validation). The recommended fix is
   "fix forward" (re-add the additive fields) rather than
   strict revert; the comparison engine is not modified by this
   PR so no producer ever populated these fields in this slice.
4. Existing `grounded_items` / `ungrounded_items` /
   `grounding_gate_result` / `grounding_gate_bypass_record`
   files in the data lake are audit-only, non-promoted, and not
   indexed; they survive revert with no schema attached (no
   schema was registered for them — they pass through the
   data-lake's append-only contract unchanged).

### Data migration required for rollback

None for pre-4.A artifacts (additive optional schema changes;
they continue to validate unchanged). Post-4.A meeting_minutes
artifacts stamped `"1.5.0"` require re-baselining if a strict
revert is taken; "fix forward" by re-adding the additive enum
value and field is the recommended path and requires no data
migration.

### Verification that the rollback is clean

```bash
pytest tests/test_grounding_gate.py
```

After revert this test file ceases to exist; the verification
is that the rest of the test suite still passes (Phase 1's
`tests/test_data_lake_grounding.py`, the Phase 3.A
`tests/test_prompt_negative_and_reason_field.py`, and the
Phase 3.B-E `tests/test_phase_3b_3e_prompt_additions.py` must
all still pass after revert — no production code path depends
on the Phase 4.A gate). The standalone
`scripts/run_grounding_gate.py` is removed by the revert; its
integration test
`tests/integration/test_run_grounding_gate_script.py` is
removed with it.

`verification_command`: `pytest tests/test_grounding_gate.py`

### Cross-PR dependency

`depends_on`: PR #235 (Phase 3.A `reason` field on the 14
claim-shaped types) and PR #236 (Phase 3.B–E taxonomy / modal
policy / glossary / few-shot sections). The Phase 4.A prompt
section is placed AFTER the Phase 3.A DO NOT EXTRACT block and
BEFORE the Phase 3.A Reason field block, and the Phase 4.A
schema additions sit alongside the Phase 3.B `decision_subtype`
enum on the decisions object. Reverting either of #235 / #236
underneath this PR would leave the Phase 4.A prompt section
referencing a missing anchor and the schema in an inconsistent
state. If all three are ever reverted, revert in reverse order:
#237 first, then #236, then #235.

`no_future_dependency`: this entry does not gate any future PR
on the prompt or schema axes. The follow-up PR that bumps
`GROUNDING_BINDING_SCHEMA_VERSION` to `"1.5.0"`, and the
follow-up PR that wires the gate inline into the orchestrator,
both depend on PR #237 being merged; their own rollback
entries will reference this PR by number.

### Operator action after merge

1. Dispatch `.github/workflows/run-grounding-gate.yml` against a
   representative source_id to confirm the new script discovers
   the right `meeting_minutes__*.json`, writes the four new
   artifacts under
   `processed/meetings/<source-id>/`, and surfaces the step
   summary on a phone screen. Use `disable_gate=false` for the
   first dispatch; the second dispatch with `disable_gate=true`
   confirms the bypass-record audit path.
2. The follow-up PR that wires the gate inline into the
   orchestrator (after `meeting-minutes-llm` writes, before
   `compare-opus-haiku` runs) will reuse this PR's
   `promotion/grounding_gate.py` module — no module-API change
   is anticipated.
3. The follow-up PR that wires
   `scripts/compare_opus_haiku.py` to populate the 13 new
   `comparison_result` fields can be authored independently;
   the schema is already in place.
4. The follow-up PR that bumps
   `promotion.gate.GROUNDING_BINDING_SCHEMA_VERSION` to
   `"1.5.0"` MUST also handle the cascade into the 1.4.0 Opus
   baseline (re-baseline or migrate) and into
   `compare_opus_haiku.py`'s legacy-filter logic; do NOT take
   that bump as a standalone one-line change.

---

## agenda_item.summary — additive optional field (PR #241)

### What this change adds

- Additive optional property on
  `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`:
  `agenda_item` items gain a new optional `summary` field
  (`type: string`, `minLength: 1`, `maxLength: 500`). The field is
  NOT added to the `required` array on `agenda_item` items, so
  pre-existing artifacts that omit `summary` validate unchanged.
- No `schema_version` bump. Same precedent as the Phase 2.C
  `position_type` enum extension above and the attendees.agency
  null fix (commit `d2e23d7`): a backward-compatible additive
  field keeps the `schema_version` enum unchanged. The
  `meeting_minutes.schema.json` `schema_version` enum
  (`1.0.0` … `1.5.0`) is unchanged.
- `additionalProperties: false` on the `agenda_item` items
  schema stays in place. The only new key permitted is `summary`;
  any other unknown field is still rejected.
- No prompt change. The strict-schema callout in
  `workflows/prompts/meeting_minutes_llm.md` (line 532) and the
  natural-language guidance in `meeting_minutes_opus.md` continue
  to list the original eight `agenda_item` fields; `summary`
  remains an opportunistic field a producer MAY emit but is NOT
  required to. No producer relies on the field being present.
- No mirror change to
  `src/spectrum_systems_core/schemas/meeting_extraction.schema.json`:
  that schema does not define `agenda_item` (verified by
  `grep '"agenda_item"' meeting_extraction.schema.json` returning
  zero hits).
- No new test file is added. The existing `agenda_item` tests in
  `tests/test_meeting_minutes_schema.py`
  (`test_agenda_item_item_number_null_validates`,
  `test_agenda_item_minimal_required_only_validates`,
  `test_agenda_item_allocated_minutes_integer_validates`) all
  pass unchanged because `summary` is optional.
- No new gate, no new eval, no new artifact type.

### Companion tooling shipped in the same PR

- `scripts/finalize_rollback_entry.py` — new automation that takes a
  PR number and rewrites the `PR #TBD` placeholder in the entry this
  branch added to `rollback_contracts.md`. Only `##`-headed
  placeholder lines added by the current branch are rewritten;
  pre-existing `PR #TBD` headings in already-merged entries and
  body-text cross-references stay untouched. Idempotent — a second
  run is a no-op.
- `scripts/_pre_pr_rollback_check.py` — strengthened Stop hook.
  Previously caught the "no entry at all" failure mode; now ALSO
  catches the "entry exists but heading still says `PR #TBD` after
  the branch was pushed" failure mode that produced verify-rollback
  CI failures on at least five PRs in a row. The check looks at
  entry HEADING lines only, so body-text cross-references to other
  PRs remain valid.
- `tests/pipeline/test_finalize_rollback_entry.py` — 5 unit tests
  pinning rewrite-scoping, idempotency, no-op, missing-file, and
  with-commit behavior.

These tooling additions are not gates and do not change the
verify-rollback-contracts CI workflow's behavior; they automate the
post-PR-open finalization step that CLAUDE.md already documents but
which sessions have been silently skipping.

### Motivation

Haiku Stage 1 extraction was BLOCKED with
`Additional properties are not allowed ('summary' was unexpected)
at path=['agenda_item', 1]`. The strict-schema gate rejected an
`agenda_item` item carrying a `summary` field. The field is a
natural emission given the Stage 1 prompt enrichment and the fact
that the semantically similar `topics` and `meeting_phases` item
types both already carry an optional `summary`. Widening the
schema to accept the field is the durable fix vs retrying the
producer.

### To roll back

1. Revert the PR. The `summary` property disappears from the
   `agenda_item` items sub-schema. The eight original properties
   (`item_id`, `item_number`, `title`, `presenter`,
   `allocated_minutes`, `start_turn_id`, `end_turn_id`, `outcome`)
   plus the two Phase 1 grounding fields (`grounding_mode`,
   `source_turn_ids`) remain as before.
2. Existing artifacts in the data-lake that carry an
   `agenda_item[].summary` (Haiku Stage 1 runs starting with the
   one that motivated this PR) become invalid against the reverted
   schema and would be blocked by the strict-schema eval on
   re-validation. The data-lake is append-only per
   `docs/contracts/data_lake_contract.md` §8; do NOT delete them.
   If revert is required, re-extract the affected source(s) so the
   `summary` field is not emitted, or accept that the superseded
   artifact is no longer re-validateable.

### Data migration required for rollback

None for the schema or codebase. Any `agenda_item[].summary`-bearing
artifact on disk falls out of strict-schema validity until either
re-extracted or until the field is re-added in a follow-up PR.

### Verification that the rollback is clean

```bash
pytest tests/test_meeting_minutes_schema.py
```

`verification_command`: `pytest tests/test_meeting_minutes_schema.py`

### Cross-PR dependency

`depends_on`: PR #182 (attendees.agency null fix) and the Phase 2.C
`position_type` enum extension (PR #TBD above; pre-existing entry) —
same fix pattern:
a semantically valid value/field emitted by a producer is widened
in the schema fail-closed rather than retried. No code dependency.

`no_future_dependency`: this entry does not gate any future PR.

### Operator action after merge

1. Re-dispatch the Haiku Stage 1 extraction workflow on the
   transcript that previously blocked. The `agenda_item` item
   that emitted `summary` now passes the strict-schema eval.
2. No other workflow needs to be re-dispatched. Downstream
   consumers (gate, comparison, promotion) read the `agenda_item`
   `item_id` / `title` fields and are unaffected by the new
   optional `summary`.

---

## Phase 4.B — precision negative examples + action_items dict shape (PR #247)

### What this change adds

- `meeting_minutes` schema bumped from 1.5.0 to 1.6.0 in the
  `schema_version` enum on
  `src/spectrum_systems_core/schemas/meeting_minutes.schema.json`.
- `action_items.items` on the meeting_minutes schema is tightened
  from `oneOf [string, object]` to `object`-only. The bare-string
  branch from 1.0.0-1.5.0 is REMOVED — every action_item must now
  be a structured object so the Phase 4.A grounding gate can verify
  its `source_quote`. The object form keeps its existing required
  field (`action`); `source_quote` remains optional at the JSON-Schema
  layer (the grounding gate is the enforcement point at runtime).
- The `Phase 4.B (schema_version 1.6.0)` description on the
  `action_items` array documents the contract and the motivation
  (bare strings rejected by the gate as `is a bare value, cannot
  carry source_quote`).
- `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
  and `meeting_minutes_opus.md` are both bumped from `version: 4.A`
  to `version: 4.B`, with a new changelog entry.
- New `Per-type precision guard (from extraction analysis)`
  subsection inside `DO NOT EXTRACT` in both prompts, byte-identical
  between the two. Negative examples are sourced verbatim from the
  haiku_only list in the 2026-05-24 comparison run (decisions,
  topics, technical_parameters, commitments, precedent_reference,
  issue_registry_entry).
- New action_items dict-shape instruction with `WRONG (bare string)`
  / `CORRECT (object)` example, byte-identical between the two
  prompts, inside the HTML-comment markers
  `<!-- ACTION_ITEMS_DICT_4B_BEGIN -->` / `<!-- ACTION_ITEMS_DICT_4B_END -->`.
- `src/spectrum_systems_core/workflows/meeting_minutes.py` regex
  workflow now emits `[{"action": <text>}]` instead of `[<text>]`
  for `action_items` so its output validates against the tightened
  1.6.0 schema.
- New test file `tests/test_phase_4b_precision_prompt.py` (10 tests)
  pinning the precision guard, the action_items dict-shape instruction,
  the byte-identical sync between the two prompts, the schema's
  object-only shape, the version bump, and the section order.
- Updates to ~12 existing test files / fixtures (and one golden
  expected.json) so the bare-string `action_items` fixtures they
  carried are now object-form fixtures; the test_grounding_prompt_section
  version check is widened to "4.x" so the per-phase test owns the
  exact-version assertion.

No new gate is added. No new artifact type is added. No new diagnostic
artifact is written. The grounding gate (already shipped in Phase 4.A)
remains the runtime enforcement point that catches bare-value items;
this PR adds the structural enforcement at the schema layer plus the
prompt-level teaching signal that prevents the producer from emitting
the failing shape in the first place.

### Motivation

The 2026-05-24 comparison run measured the post-Stage-1 / post-Phase-4.A
Haiku extraction against the Opus reference baseline:

- Haiku F1 vs Opus: 37.8% (recall 70.1%, precision 25.9%)
- Haiku emitted 263 items vs Opus 97 — 2.7× over-extraction
- Worst per-type over-extraction: topics 3.7×, commitments 5.5×,
  decisions 3.0×, technical_parameters 3.0×, precedent_reference 3.0×

Precision is the bottleneck. The DO NOT EXTRACT section that landed
in Phase 3.A is too generic to constrain the specific haiku_only false
positives observed. Phase 4.B adds per-type guards keyed on the actual
false positives so the model sees a concrete pattern (procedural
"finishing before 1:00", tentative "we're probably going to make a
change") it should NOT extract.

Separately, the grounding-gate run produced
`action_items[0]: is a bare value, cannot carry source_quote` on
every bare-string action_item, blocking the artifact. The fix is two
parts: (1) the prompt teaches the model to emit dicts (with a WRONG /
CORRECT demonstration so the rule is grounded in a concrete shape),
and (2) the schema is tightened to disallow bare strings so a future
regression cannot reintroduce them.

### To roll back

1. Revert the PR. The `Per-type precision guard` subsection
   disappears from both prompts. The action_items dict-shape
   instruction and its WRONG / CORRECT example disappear from both
   prompts. The version field in both prompts reverts to `4.A`.
2. The schema's `action_items.items` reverts to the `oneOf [string,
   object]` shape. The `1.6.0` enum value is removed from
   `schema_version`.
3. The regex `meeting_minutes.py` workflow reverts to emitting
   bare-string `action_items`.
4. Existing 1.6.0 artifacts in the data-lake (any Phase 4.B run)
   become re-validatable against the reverted (1.5.0) schema only if
   their `schema_version` is rewritten to `1.5.0` AND their
   `action_items` are bare strings — which they are NOT (Phase 4.B
   producers emit objects). On revert, those object-form artifacts
   still validate (the 1.5.0 schema's `oneOf` branch accepted both
   shapes), but their `schema_version` field's `1.6.0` value is
   suddenly out of enum. Either bump the data-lake artifacts' field
   back to `1.5.0` OR accept that the Phase 4.B runs are no longer
   re-validatable until the schema is re-extended.
5. Existing pre-Phase-4.B artifacts that carried bare-string
   action_items (e.g. legacy regex-workflow output) become valid
   again — they were valid pre-4.B, blocked under 4.B (since the
   regex workflow was migrated in lockstep), and valid again on
   revert.

### Data migration required for rollback

None for the schema or codebase. Any 1.6.0 `schema_version` value on
a data-lake artifact falls out of enum validity until either rewritten
to 1.5.0 or the enum is re-extended in a follow-up PR. The data-lake
is append-only per `docs/contracts/data_lake_contract.md` §8; do NOT
delete the affected artifacts.

### Verification that the rollback is clean

```bash
pytest tests/test_phase_4b_precision_prompt.py tests/test_meeting_minutes_schema.py tests/test_minimal_workflow.py
```

`verification_command`: `pytest tests/test_phase_4b_precision_prompt.py`

### Cross-PR dependency

`depends_on`: PR #237 (Phase 4.A G-GROUND-VERBATIM) — Phase 4.B's
schema tightening and prompt instruction both reference the
grounding gate added in 4.A. Without 4.A's gate, the bare-value
rejection that motivates the dict-shape rule does not exist. No
code dependency beyond the prompt-section adjacency.

`no_future_dependency`: this entry does not gate any future PR.

### Operator action after merge

1. Re-dispatch the Haiku Stage 1 extraction workflow. The new prompt
   instructs the model to emit `action_items` as dicts so the
   grounding gate can verify `source_quote`. Precision is expected
   to improve +8-12 pts (per the 2026-05-24 comparison-run
   projection in the PR description).
2. Re-run `compare_opus_haiku.py` against the new extraction to
   measure the F1 / precision / recall delta. The Stage 1 gate is
   F1 ≥ 39.5% (still below pre-4.B baseline) and the Stage 1
   ceiling is F1 ≥ 55%; the expected post-4.B F1 lands at 45-50%.
3. No other workflow needs to be re-dispatched. The control
   function and promotion gate are unchanged — Phase 4.B touches
   the producer (the prompt) and the schema's structural shape, not
   the governance gate.

---

## Phase 4.C — per-item Sonnet cascade filter on grounded items (PR #246)

### What this change adds

- New module `src/spectrum_systems_core/promotion/cascade_filter.py`
  implementing the Phase 4.C cascade. Per-item Sonnet adjudication
  of items that cleared the Phase 4.A grounding gate. Decisions are
  `keep` / `drop` / `modify`. The module is pure except for the
  injectable `api_client` callable.
- New prompt template
  `src/spectrum_systems_core/workflows/prompts/cascade_filter.md`.
- New CLI `scripts/run_cascade_filter.py` driven by the
  `.github/workflows/run-cascade-filter.yml` workflow. Reads the
  `grounded_items__<run_id>.json` artifact (Phase 4.A output) and
  the matching `grounding_gate_result__<run_id>.json` (race-condition
  guard), writes four artifacts under the same processed/meetings
  directory: `cascade_filtered__<run_id>.json`,
  `cascade_audit__<run_id>.jsonl`,
  `cascade_filter_result__<run_id>.json`, and (only on
  `--disable-cascade`) `cascade_bypass_record__<run_id>.json`.
- The Phase 2.C cascade workflow
  `.github/workflows/run-cascade-filter.yml` is renamed to
  `.github/workflows/run-cascade-filter-phase-2c-deprecated.yml`
  with a deprecation banner at the top. The Phase 2.C cascade
  module at `src/spectrum_systems_core/cascade/` is untouched —
  it operates on raw Haiku extractions, not on grounded items, and
  is preserved for audit trail and any in-flight Phase 2.C run that
  has not been re-baselined. New precision work uses Phase 4.C.
- Per-type disqualifier parser in
  `promotion/cascade_filter.py::parse_type_disqualifiers` reads the
  Phase 4.B `<!-- PRECISION_GUARD_4B_BEGIN -->` block from
  `meeting_minutes_llm.md` and routes each `**type** —` paragraph to
  the matching claim-shaped type. When the block is absent (older
  prompts) the parser falls back to the `DO NOT EXTRACT` section.
- Additive cascade fields on the `comparison_result` artifact:
  `pre_cascade_haiku_count`, `pre_cascade_haiku_f1`,
  `pre_cascade_haiku_precision`, `pre_cascade_haiku_recall`,
  `post_cascade_haiku_count`, `post_cascade_haiku_f1`,
  `post_cascade_haiku_precision`, `post_cascade_haiku_recall`,
  `cascade_kept_count`, `cascade_dropped_count`,
  `cascade_modified_count`, `cascade_drop_rate`,
  `cascade_recall_collapse_warning`. Each field is optional; absence
  means "the cascade did not run for this source". The
  Phase 2.C `production_haiku_with_cascade_filter` enum value stays
  unchanged (Phase 2.C's cascade still uses it).
- New constants in the cascade module:
  `CASCADE_FILTER_MODEL = "claude-sonnet-4-6"`,
  `CASCADE_FILTER_SCHEMA_VERSION = "1.0.0"`,
  `CASCADE_BATCH_SIZE = 10`, `CASCADE_MAX_BATCHES_DEFAULT = 30`.
- No `schema_version` bump on `meeting_minutes.schema.json` or
  `comparison_result.schema.json` — the cascade fields are additive
  optional and follow the same lifecycle as the Phase 4.A gate
  fields (PR #237).
- No change to `promotion/gate.py` or `promotion/grounding_gate.py`
  — the cascade reads the gate's output but does not modify the
  gate itself. `GROUNDING_BINDING_SCHEMA_VERSION = "1.4.0"` and
  `GROUNDING_GATE_SCHEMA_VERSION = "1.5.0"` are untouched.

### Motivation

After the Phase 4.A grounding gate landed (PR #237) every item the
extractor emits is guaranteed to be a verbatim transcript substring.
That defends against hallucination but not against over-extraction:
the recall-oriented Haiku prompt still emits items that are
correctly typed by shape but wrong by content (brainstorming
restated as decisions, the agenda restated as action items, etc.).
The cascade is the precision pass on top of grounding: Sonnet reads
each grounded item plus its `source_quote` and decides whether the
item belongs in its claimed extraction type. Phase 4.B (PR #247)
added the per-type disqualifier prompt section that the cascade
reads as its precision signal source.

The Phase 2.C cascade (PR #221) operated on the raw Haiku
extraction, mixing the precision signal (drop over-extracted items)
with the noise signal (drop hallucinations). The grounding gate now
absorbs the noise signal cleanly; the new cascade is therefore a
narrower, higher-precision filter than Phase 2.C ever could be.

### To roll back

1. Revert the PR. The `cascade_filter.py` module, the prompt, the
   script, and the workflow disappear. The Phase 2.C
   `run-cascade-filter.yml` and its accompanying `cascade/`
   module remain in place — they were not modified — so any Phase
   2.C automation in flight stays functional. The Phase 2.C
   workflow that was renamed to
   `run-cascade-filter-phase-2c-deprecated.yml` is automatically
   restored to `run-cascade-filter.yml` by the revert.
2. The Phase 4.C cascade artifacts on disk (`cascade_filtered__*`,
   `cascade_audit__*`, `cascade_filter_result__*`,
   `cascade_bypass_record__*`) become orphans. The data-lake is
   append-only per `data_lake_contract.md` §8; do NOT delete them.
   They simply have no reader after the revert.
3. The cascade fields on existing `comparison_result` artifacts
   become unknown-but-optional keys against the reverted schema.
   The schema's `additionalProperties: false` will reject these
   artifacts on re-validation; either re-run the comparison
   without the cascade fields (the reverted code path will not
   emit them) or accept the stored artifacts as no-longer-valid.

### Data migration required for rollback

None for code. Cascade artifacts on disk become orphan reads but
the gate (Phase 4.A) and the upstream extraction are unaffected.

### Verification that the rollback is clean

```bash
pytest tests/test_cascade_filter.py tests/integration/test_run_cascade_filter_script.py tests/test_cascade_comparison_artifact.py
```

After the revert each of those test modules disappears, so the
verification command above runs against the test files that are
still present and reports zero failures. The Phase 4.A grounding
gate tests (`tests/promotion/test_grounding_gate.py` and
`tests/integration/test_run_grounding_gate_script.py`) MUST continue
to pass post-revert — the cascade was a strict superset of
governance, never a precondition for the gate.

`verification_command`: `pytest tests/test_cascade_filter.py`

### Cross-PR dependency

`depends_on`: PR #237 (Phase 4.A G-GROUND-VERBATIM) and PR #247
(Phase 4.B precision guard). The cascade reads the grounding gate's
`grounded_items__<run_id>.json` output and the matching
`grounding_gate_result__<run_id>.json` and refuses to run without
both. The Phase 4.B `<!-- PRECISION_GUARD_4B_BEGIN -->` block in
`meeting_minutes_llm.md` is the cascade's per-type disqualifier
source (with a fallback to `DO NOT EXTRACT` if the markers are
absent).

`no_future_dependency`: the cascade does not gate Phase 4.D
(decision sub-pass) or Stage 3 (per-type confidence thresholds,
self-consistency). Those phases consume `cascade_filtered__*.json`
artifacts but the data-lake contract permits a Stage 3 reader to
fall back to `grounded_items__*.json` when no cascade artifact
exists.

### Operator action after merge

1. Re-dispatch `run-cascade-filter.yml` on the most recent
   grounded artifact for the 7-GHz Dec 18 transcript and read the
   step summary: drop rate, top drop reasons, recall-collapse
   warning. Calibration target: 20-50% drop rate against a fresh
   Haiku run, < 5% drop rate against an Opus baseline.
2. Re-dispatch `run-comparison.yml` with `use_cascade_output=true`
   to see the post-cascade F1 vs Opus. The comparison artifact
   will carry the new cascade fields once the cascade has run.

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
