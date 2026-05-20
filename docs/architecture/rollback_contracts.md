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
