# Phase Q — Extraction Quality Pass (few-shot + omit + confidence) progress

## Prerequisites (recorded)

- `.claude/settings.json` contains `dangerouslySkipPermissions: true` ✓
- Baseline pytest collect-only (before Phase Q): **1028 tests**
- Branch: `claude/extraction-quality-improvements-ODsXW` (off main)

## Findings from prerequisites check

- `FewShotLoader` + `format_examples_for_prompt` already exist at
  `src/spectrum_systems_core/evals/m4/few_shot.py`. They are
  version-gated, type-filterable, and ship with passing tests under
  `tests/eval/test_few_shot.py`. The task spec asked for a new
  `extraction/few_shot_loader.py`; reusing the existing loader avoids
  duplication. Wiring goes directly from each extractor into the M.4
  loader.
- `extraction_few_shot_v1.json` exists at
  `contracts/eval/seeds/extraction_few_shot_v1.json` (not
  `SDL_ROOT/few_shot/` as the spec phrased). The loader resolves both
  locations.
- No `extraction_run_record` artifact exists. The closest is
  `meeting_extraction` (one per run, written by `ExtractionMerger`).
  Per operator decision, the new run-level fields are added to
  `meeting_extraction` (schema bumped to 1.1.0) rather than to a new
  sibling artifact.
- No stored `meeting_extraction` artifacts on disk -- safe to bump the
  schema and add `confidence` as a REQUIRED item field with no
  migration burden.

## Build plan

| Part | Deliverable                                                                          | Status |
| ---- | ------------------------------------------------------------------------------------ | ------ |
| A    | Few-shot injection wired through all three typed extractors                          | ✓      |
| B    | OMIT constraint block in every extraction prompt (position-asserted)                 | ✓      |
| C    | `confidence` required on items + threshold flag + low_confidence_flagged on aligner  | ✓      |
| D    | Prompt overload audit (see "PROMPT OVERLOAD FINDINGS" below; no refactor this phase) | ✓      |
| E    | New tests: `test_few_shot_loader.py`, `test_omit_constraint.py`, `test_confidence.py`| ✓      |

## PROMPT OVERLOAD FINDINGS (Part D -- documented, NOT fixed)

Per Steve Kinney 2026 -- "If your prompt is doing five things, it should
be five prompts" -- counting distinct instruction *types* in each
post-refactor extractor prompt (sub-rules of one constraint count once):

1. **DecisionExtractor** -- 6 distinct instruction types:
   1. Extract decisions from chunks (primary task)
   2. Honor OMIT constraint (hallucination guard, 5 sub-rules)
   3. Apply glossary terminology (read-only reference)
   4. Imitate few-shot pattern (reference)
   5. Produce JSON with controlled vocabulary + cite source_turn_ids
   6. Score confidence per item
   -> **PROMPT OVERLOAD: DecisionExtractor has 6 distinct instructions**

2. **ClaimExtractor** -- 6 distinct instruction types (same shape as
   decision, minus controlled-vocab nuance, plus required speaker).
   -> **PROMPT OVERLOAD: ClaimExtractor has 6 distinct instructions**

3. **ActionItemExtractor** -- 6 distinct instruction types (same shape,
   plus required owner attribution).
   -> **PROMPT OVERLOAD: ActionItemExtractor has 6 distinct instructions**

All three exceed the >4 threshold from the Kinney finding. Documented
here as the seed input for the next-phase work (Phase Q+1) on two-stage
extraction (split each prompt into "extract" + "validate"). Not fixed in
this PR by design -- a refactor that large would obscure the
quality-only changes this PR carries.

## Test counts

- Baseline (pre-Phase-Q): 1028 tests collected.
- After Phase Q: 1068 tests collected (40 new).
- All 40 new tests pass.
- Full suite (excluding `tests/ingestion/` pre-existing PDF env failures):
  909 passed, 0 failures.

## Red Team passes

- Pass 1: 2 Sev-2 findings fixed (vacuous few-shot injection from
  format_examples_for_prompt's header-only block; `omit_instruction_present`
  made post-render instead of a decorative constant). 3 regression-guard
  tests added.
- Pass 2: no blocking findings on tests (real code paths exercised,
  no loader mocking, positional checks numerical not substring-only).
- Pass 3: ready to PR.

# Phase P — Operational Verification Cycle (Safety Nets) progress

## Prerequisites (recorded)

- `.claude/settings.json` contains `dangerouslySkipPermissions: true` ✓
- Baseline pytest collect-only (before Phase P): **998 tests**
- Phase O CLI commands present: `verify-pipeline-state`, `compile-findings`,
  `review-baseline-candidate`, `eval-ground-truth` ✓
- `run-pipeline.yml` workflow present ✓
- `migrate-artifact-kind.yml` workflow present ✓
- Branch: `claude/phase-p-safety-nets-WBrFR` (off main)

## Build plan

| Part | Deliverable                                                                            | Status |
| ---- | -------------------------------------------------------------------------------------- | ------ |
| A    | `check-preflight` CLI + run-pipeline.yml pre-flight step + `--no-write-artifact` flag  | ✓      |
| B    | `review-baseline-candidate` absolute-minimum floor (< 50) + `--set-baseline` reminder  | ✓      |
| C    | `next-phase-handoff` CLI + `next_phase_briefing` schema                                | ✓      |
| D    | `docs/runbooks/verification-cycle-recovery.md`                                         | ✓      |
| E    | Tests under `tests/cli/` + `tests/runbooks/`                                           | ✓      |

## Test counts

- Baseline (pre-Phase-P): 998 tests collected.
- After Phase P: 1028 tests collected (30 new).
- All 30 new tests pass.
- Full suite (excluding `tests/ingestion/` PDF env failures pre-existing
  from the Phase O era): 869 passed, 0 failures.

## Red Team passes

- Pass 1: no blocking Sev-1 or Sev-2 findings.
- Pass 2: no blocking findings.
- Pass 3: ready to PR.

# Phase O — End-to-End Verification Cycle progress (prior)

## Prerequisites (recorded)

- Baseline pytest collect-only: **966 tests**
- After Phase O: 997 tests collected.
