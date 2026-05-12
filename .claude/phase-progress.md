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
