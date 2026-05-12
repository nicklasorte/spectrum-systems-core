# Phase V — Post-Hoc Source Verification — Progress

## Prerequisites findings

- `.claude/settings.json` contains `{"dangerouslySkipPermissions": true}`: confirmed.
- `model_registry.json` does NOT exist at SDL_ROOT/config/. The codebase uses
  `ai/registry/prompts.json` via `PromptRegistry` instead. The current
  prompt registry defines task types {memory_query, claim_check,
  objection_check, story_fit}; no "generation" task_type yet.
  Decision: implement Phase V to consume a small `ModelRegistry` shim
  that resolves `get("generation")` from the prompts registry if present
  and falls back to a documented default model id. Surface in the PR
  description that `seed_model_registry` is the prerequisite for live
  verification on real data. Phase V build is feature-flag-disabled by
  default so this absence does not block the build.
- meeting_extraction artifacts in SDL_ROOT/extractions/: no SDL_ROOT live
  data is available in this build environment. Tests use fixture chunks.
  Pipeline wiring is gated on the feature flag so absence at build time
  is acceptable; runtime PostHocVerifier still works on any extraction.
- chunks.jsonl files: build env has no SDL_ROOT pipeline data. Fixture
  chunks under tests/fixtures suffice for tests.
- Baseline pytest collect: 1123 tests.
- Existing "Gate-2" implementation: no literal Gate-2 exists. The
  closest gate today is `RegressionGate` (m4/regression_gate.py) and
  the M3 routing-quality warning. Decision: implement Phase V's
  "Gate-2 check" as a NEW `VerificationGate` under
  `verification/verification_gate.py` returning a `GateDecision`
  dataclass. The existing `RegressionGate` is extended with
  `compute_spurious_add_rate` and a separate `check_spurious_add_regression`
  classmethod so the two metrics are wired in one place.
- `ExtractionMerger` produces items with `items_requiring_review=True`
  and `review_reason="low_confidence"`. There is no
  `pending_review_extraction` artifact today. Decision: keep ONE
  artifact (meeting_extraction), tag failed-verification items with
  `items_requiring_review=True` and append the post-hoc reason into
  a new `exclusion_reasons: list[str]` field on each item.
  This is the "same artifact, same queue, multiple reasons" outcome
  the spec requires.
- Existing artifact_validator only handles artifact_kind/artifact_type
  deprecation — it does not run JSON schema validation. Phase V adds
  a thin validator inside the verification module.

## Decisions

1. `VerificationGate` is a new module returning `GateDecision`.
2. `ModelRegistry` is a thin adapter; default model id is recorded in code.
3. PostHocVerifier accepts an injected `api_caller`/`verifier_caller` so
   tests can mock the LLM, matching the existing extractor convention.
4. Feature flag artifact lives at SDL_ROOT/store/artifacts/config/
   `phase_v_post_hoc_verification_enabled.json`.
5. meeting_extraction schema bumps to "2.0.0" only on the new write path
   (when Phase V flag is enabled). Old v1.1.0 artifacts remain valid.

## Red Team passes

### Pass 1 — silent successes, bypassable gates

Sev-1 (fixed): runner Phase V hook gated on `data_lake` kwarg only;
env-driven callers could bypass. Fixed by resolving from
`DATA_LAKE_PATH` before the flag check.

Sev-1 (fixed): `ExtractionMerger.write_to` validated against v1 schema
only; a v2 artifact missing `verification_status` could land on disk.
Fixed with a v2 validation call inside `write_to`.

Sev-2 (fixed): `pipeline_run_id` schema field is uuid, runner passed
`tex-<hex>` string. Fixed by coercing non-uuid inputs to a deterministic
uuid5 + enabling Draft202012 format-checker.

Sev-2 (fixed): halt-path completeness check could pass with 0 entries.
Fixed by requiring >= EARLY_HALT_SAMPLE_SIZE on halt.

Sev-2 (fixed): VerificationGate breakdown all-zeros for unknown status.
Fixed by adding `"unknown"` bucket.

### Pass 2 — tests validate the right failure mode

Sev-2 fixes: `test_verified_item_not_added_to_hitl_queue`,
`test_pipeline_skips_verification_when_flag_disabled`,
`test_uses_generation_task_type_not_extraction`,
`test_pipeline_halts_when_verification_halted` rewritten to assert on
specific state / disk artifacts rather than absent-key short-circuits.

### Pass 3 — rejection tests for every gate

All eleven gates (A-K), plus flag-disabled rollback, v1 readability,
and API-failure degradation paths each map to a Python test that
asserts the SPECIFIC failure mode. Verdict: ready to PR.

## Test counts

- Baseline: 1123 tests
- Final: 1193 tests (70 new for Phase V)
- 3 pre-existing PDF failures unrelated to Phase V (local
  cryptography/cffi env issue; confirmed against `git stash`).

## Smoke test

`python scripts/smoke_test_fixture.py --enable-phase-v` runs the verifier
on the 10-chunk fixture transcript with a deterministic stub that returns
`verified` once and `unsupported` for the rest. Output:

    Verification artifact: verified=1, unsupported=9, total=10
    PHASE V SMOKE TEST PASSED

---

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

---

# Phase Infra — Automated CI checks (branch: claude/automated-ci-checks-7oQIe)

## Prerequisites (recorded 2026-05-12)

1. `.claude/settings.json` has `dangerouslySkipPermissions: true` ✓
2. pytest baseline: **1123 tests collected** (after `pip install -e ".[dev]"`).
3. `grep -rn 'claude-sonnet-4-20250514' --include='*.py' .` — **9 hits, 5 files**:
   - `src/spectrum_systems_core/synthesis/report_generator.py:28`
   - `src/spectrum_systems_core/synthesis/keynote_generator.py:27`
   - `src/spectrum_systems_core/paper/revision_workflow.py:3` (docstring)
   - `src/spectrum_systems_core/paper/revision_workflow.py:29`
   - `tests/synthesis/test_keynote_eval.py:39,174`
   - `tests/synthesis/test_grounding_eval.py:57,132`
   - `tests/synthesis/test_report_generator.py:135`
4. `grep -rn '"artifact_kind"' contracts/schemas/` — **3 schemas**:
   - `contracts/schemas/source_record.schema.json`
   - `contracts/schemas/review_artifact.schema.json`
   - `contracts/schemas/obsidian_input_artifact.schema.json`
5. Workflow YAML validity: all 10 workflows parse OK.
6. `grep -rn 'claude-' --include='*.yml' .github/workflows/` — **0 hits**.

## Decisions

- **Check 1**: Grandfather the 5 legacy files via `ALLOWED_LOCATIONS`
  (user-approved). No `model_registry.py` exists yet, so it is NOT listed
  as an allowed location. The check still blocks any NEW occurrence of a
  deprecated string outside the grandfathered set. Migration of the legacy
  files is tracked separately.
- **Check 2**: Grandfather the 3 pre-migration schemas. New schemas using
  `artifact_kind` will fail the check.
- **Checks 3 & 4**: No violations — write tests directly.
- **PyYAML**: already in main `dependencies` (`>=6.0`), not duplicated in dev.
