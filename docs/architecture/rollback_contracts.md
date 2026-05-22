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

## Phase 2.B — chunk overlap (PR #XXX)

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
- Two new optional aggregate fields on
  `chunk_merge_summary.json`/`chunk_split_summary.json`:
  `overlap_turns_prepended_total: int`,
  `overlap_clamped_count: int`.
- New workflow `.github/workflows/run-haiku-overlap-extraction.yml`
  mirroring `run-haiku-extraction.yml` but passing
  `CHUNK_OVERLAP_TURNS=2`. Phone-safe operator-driven dispatch.
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
python scripts/verify_rollback_contracts.py --pr XXX
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
