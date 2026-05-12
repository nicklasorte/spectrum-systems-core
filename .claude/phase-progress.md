# Phase Infra — Wire pipeline automation

## Prerequisites (recorded)

- `.claude/settings.json` contains `dangerouslySkipPermissions: true` ✓
- Branch: `claude/wire-pipeline-automation-EHap3` (per session instructions)
- `scripts/seed_glossary.py` present ✓ (flags: `--out`, `--deterministic` only;
  no `--force` or `--data-lake` — re-running overwrites in place, so `force`
  semantics already hold; workflow uses `--out "$SDL_ROOT/glossary"`).
- `.github/workflows/eval-ground-truth.yml` present ✓ (name: `Eval ground truth`)
- `.github/workflows/migrate-artifact-kind.yml` present ✓ (name: `migrate-artifact-kind`)
- `.github/workflows/run-pipeline.yml` present ✓ (name: `Run pipeline`)

### Workflow name → trigger consistency table

| File                       | `name:` field                          | Used in workflow_run triggers |
|----------------------------|----------------------------------------|-------------------------------|
| run-pipeline.yml           | `Run pipeline`                         | eval-ground-truth.yml         |
| eval-ground-truth.yml      | `Eval ground truth`                    | (none)                        |
| migrate-artifact-kind.yml  | `migrate-artifact-kind`                | (none)                        |
| smoke-test.yml             | `Extraction Smoke Test`                | (none)                        |
| cleanup-data-lake.yml      | `Data lake cleanup`                    | (none)                        |
| cleanup-duplicate-pairs.yml| `Cleanup duplicate ground truth pairs` | (none)                        |
| confirm-pairs.yml          | `Confirm ground truth pairs`           | (none)                        |
| verify-pipeline-state.yml  | `Verify pipeline state`                | (none)                        |
| pytest.yml                 | `pytest`                               | (none)                        |
| seed-glossary.yml (new)    | `Seed glossary terms`                  | (none)                        |

`eval-ground-truth.yml`'s `workflow_run.workflows: ["Run pipeline"]` matches the
`run-pipeline.yml` `name:` field exactly.

---

# Phase ChunkClassifier-Fix — Zero typed extractions on all transcripts

## Step 1 — Diagnosis (branch: claude/fix-chunk-classifier-i1foz)

**Symptom**: `extract-typed` on all 13 transcripts produces
`decisions=0 claims=0 action_items=0 off_topic=209/211` (etc.). The two
non-off_topic chunks per transcript are rescued by the regulatory verb
fallback, but produce zero downstream decisions.

**Q1 — What text does the classifier send to the LLM?**
`chunk["text"]`. `chunk_classifier.py::ChunkClassifier._build_prompt` (line 87)
reads `(chunk or {}).get("text", "")`. Field name is correct.

**Q2 — Classifier prompt template** (chunk_classifier.py:88-97):
```
Classify the following meeting speaker-turn into exactly one of: decision,
claim, action_item, off_topic. Return JSON {"classification": "<one>",
"confidence": <0..1 or null>}. Use 'decision' only when the group reaches or
records an explicit outcome (approved/rejected/deferred/noted/considered).
Use 'claim' for factual or technical assertions. Use 'action_item' for tasks
assigned to a named owner. Otherwise use 'off_topic'.

---
{text}
---
```

**Q3 — Regulatory verb fallback** (chunk_classifier.py:99-114):
Reads `chunk["text"]` (passed as `chunk_text`), case-insensitive word-boundary
regex. Verbs: approved, rejected, deferred, noted, considered,
action required/action_required, agreed, consensus. Field name is correct.

**Q4 — On LLM error**: returns `"off_topic"` **silently** — the bare
`except Exception` at chunk_classifier.py:157-159 swallows the error
with no log.

**Q5 — Is a real Haiku API call happening? NO.**
The `extract-typed` CLI calls `run_typed_extraction(sid, data_lake, force)`
at `cli.py:2570` without passing `api_callers`. Inside
`typed_extraction_runner.py:184-188`, `api_callers={}` so all four components
are constructed as `ChunkClassifier(api_caller=None)` etc. With `api_caller=None`,
each component falls back to its module-level `_default_api_caller`, which
**always returns the offline default** (`{"classification": "off_topic"}` /
`{"items": []}`). No HTTP call is ever made.

This is the same pattern `StoryExtractor` had to solve: `story_extractor.py:170-176`
lazy-builds a real `anthropic.Anthropic()` client when no caller is injected.
`ChunkClassifier`, `DecisionExtractor`, `ClaimExtractor`, `ActionItemExtractor`
were never given that pattern, and the runner doesn't build it for them either.

**Q6 — `chunks.jsonl` field shape**: confirmed in existing tests
(`test_typed_extraction_runner.py:102-105`, `test_chunk_classifier.py:48-50`)
that real chunks carry `{"chunk_id": "...", "text": "...", "source_id": "..."}`.
The classifier is reading the right field — there is just nothing on the other
end of the api_caller seam in production.

**Root cause**: the production code path never wires a real LLM caller into
the four typed-extraction components. Everything below the runner is correct;
the seam at `typed_extraction_runner.run_typed_extraction` needs to lazy-build
real Anthropic callers when none are injected and `ANTHROPIC_API_KEY` is set,
matching the `StoryExtractor` pattern.

**Fix shape (Step 2)**: extend `typed_extraction_runner.py` to lazy-build a
real Haiku `api_caller` per missing component. Add a small JSON-parsing helper
(tolerant of markdown code fences). Add `logging.warning` to surface
LLM-call errors instead of swallowing them. No changes to the four components'
public API — tests that inject `api_caller` are unchanged.

---

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

# Phase: fix anthropic SDK install (current)

Branch: `claude/fix-anthropic-sdk-install-KpD2d`

## Step 1 — Diagnosis

- **Is `anthropic` in project dependencies (pyproject.toml)?** No. The
  `dependencies` array in `pyproject.toml` lists only `jsonschema`,
  `PyYAML`, `pdfminer.six`, `python-docx`, `scikit-learn`, `scipy`. No
  `requirements.txt` exists at the repo root.
- **What does the install step run?**
  - `run-pipeline.yml`: `pip install -e ".[dev]"`
  - `smoke-test.yml`: `python -m pip install -e ".[dev]"`
  - `eval-ground-truth.yml`: `pip install -e ".[dev]"`
- **Is `anthropic` in `requirements.txt`?** No `requirements.txt` exists.

Root cause: `pip install -e ".[dev]"` only installs declared dependencies
plus the `dev` extra (`pytest`). Since `anthropic` is not declared, it is
never installed, and `ChunkClassifier` falls back to offline defaults
(`typed_extraction_anthropic_sdk_missing`).

## Step 2 — Fix applied

- Fix A: Added `"anthropic>=0.40.0"` to the `dependencies` array in
  `pyproject.toml`.
- Fix C: Added an explicit `python -m pip install anthropic` safety-net
  line to the `Install dependencies` step in
  `.github/workflows/run-pipeline.yml`.
- Fix D: Same safety-net line added to
  `.github/workflows/smoke-test.yml`.
- Fix E (eval-ground-truth.yml): not applied. `grep -rn anthropic
  src/spectrum_systems_core/evals/` returns nothing; the
  `eval-ground-truth` CLI command does not import `anthropic`.

## Step 3 — Local verification

After `pip install anthropic`:
`python -c "import anthropic; print(anthropic.__version__)"` succeeds.

## Stop-condition reminders

- The `ANTHROPIC_API_KEY` repository secret must be set
  (Settings → Secrets → Actions). Without it, the SDK is importable
  but real API calls will fail.

---

# Phase Perf — Pipeline parallelization + batch + async + cache

## Prerequisites recorded (2026-05-11)

- `.claude/settings.json` contains `dangerouslySkipPermissions: true` ✓
- Branch (per session instructions): `claude/perf-pipeline-parallelization-9sSeo`
- Pytest baseline: **1085 tests collected** (`python -m pytest --collect-only -q`)
- `anthropic` SDK version installed: 0.101.0 — `from anthropic import AsyncAnthropic` succeeds ✓

### Key files reviewed

- `src/spectrum_systems_core/extraction/chunk_classifier.py`
  - Single-chunk classifier with regulatory-verb fallback.
  - Exposes `classify(chunk, source_id)` only.
  - Default api_caller is offline (returns `off_topic`).
  - Model: `claude-haiku-4-5-20251001`.

- `.github/workflows/run-pipeline.yml`
  - Single sequential job `run-pipeline` runs everything.
  - Steps: install → preflight → run-pipeline → extract-typed --all → link-ground-truth → commit.

- `src/spectrum_systems_core/cli.py`
  - Uses `argparse`, NOT `click`. Subparsers via `_build_parser` and dispatch in `main()`.
  - `extract-typed --source-id <sid>` already runs typed extraction for ONE source_id.
  - `run-pipeline --specific-source-id <sid>` already runs the orchestrator for ONE source_id.
  - The slugify rule lives in `pipeline_orchestrator._slugify`:
    `lower → spaces→'-' → [^a-z0-9_-]→'-' → strip('-_')` (preserves underscores!).

- `src/spectrum_systems_core/extraction/typed_extraction_runner.py`
  - `run_typed_extraction(source_id, ...)` is the per-source entry point.
  - Currently calls `classifier.classify(chunk, source_id)` per chunk in a loop.

## Implementation notes

- **list-source-ids**: re-use `pipeline_orchestrator._slugify` so source_ids match
  exactly what ingestion uses. Filter `*minutes*` filenames to mirror orchestrator
  behavior. Walk both `.docx` and `.txt`.
- **run-single**: thin wrapper that delegates to `run_pipeline(specific_source_id=…)`.
  This avoids duplicating orchestrator logic and guarantees identical behavior.
- **Matrix workflow**: keep existing single-job flow as the post-pipeline path; split
  per-transcript work into the matrix.
