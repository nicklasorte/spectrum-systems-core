# Fix Pass #3 — Response to Red Team Review #3

Document ID: SSC-FIX-003
Scope: Resolutions for findings in `ssc_next_phase_redteam_3.md`.

---

## must_fix

### M1. `extract.py` had four near-duplicate grounders — FIXED

**Change**: replaced `_ground_meeting_minutes`,
`_ground_decision_brief`, `_ground_agency_question_summary`,
`_ground_meeting_action_log` with a single `_ground_by_prefix_table`
function plus a `_GROUNDING_PREFIXES` config table keyed by
`workflow_name`. The exposed `GROUNDED_EXTRACTORS` dict still maps
`name -> (base_extract, grounder)` so `pipeline.py` and the existing
tests are unchanged.

Before: ~80 lines of repeated loop bodies.
After: one 12-line function and a 16-line table.

**Tests**:
- `test_grounding_still_produces_correct_kinds_for_meeting_minutes`
- `test_grounding_still_produces_correct_kinds_for_decision_brief`
- (existing tests in `test_data_lake_grounding.py` regress unchanged)

---

## should_fix

### S1. Manifest/debug filename patterns were inlined — FIXED

**Change**: added `manifest_filename(run_id)`, `debug_filename(run_id)`,
and `is_run_metadata_filename(name)` to `paths.py`. `pipeline.py` and
`index.py` now call these helpers; the literal `manifest__` and
`debug__` prefixes only appear once each, in `paths.py`.

**Tests**:
- `test_manifest_and_debug_filenames_use_helpers`
- `test_is_run_metadata_filename_recognizes_both_prefixes`

### S2. Non-transcript sources could promote empty-content artifacts — FIXED

**Change**: added `_content_signal_eval` to `pipeline.py`. For
`source_type` in `{"notes", "summary"}`, the eval inspects the artifact
type's content keys (e.g., `decisions`, `action_items`,
`open_questions` for `meeting_minutes`). If every content key is empty,
the eval fails with reason code `empty_content_signal`, which the
existing control function blocks on. Transcript-sourced runs continue to
be governed exclusively by `transcript_evidence`.

**Tests**:
- `test_content_signal_blocks_empty_notes_source`
- `test_content_signal_passes_when_notes_source_has_real_content`
- `test_content_signal_does_not_apply_to_transcript_source`

### S3. Golden suite covered only `meeting_minutes` — FIXED

**Change**: added `tests/fixtures/golden_meetings/m-golden-inquiry/`
(transcript + metadata + expected) for `agency_question_summary`. The
new fixture exercises a different workflow extractor and grounder path,
giving golden coverage of two of the four workflow types. The other two
remain covered by their unit tests in
`tests/test_decision_brief_workflow.py` and
`tests/test_meeting_action_log_workflow.py`.

**Tests**: `test_golden_inquiry_promotes_with_expected_payload`.

---

## defer_with_reason

### D1. Failure-record persistence — DEFERRED

The constitution defers human review of `eval_case_candidate`. Persisting
failure records now would invent a layout the review path may not want.
Reason: defer until the review path is designed; the seed already
constructs and returns the artifacts.

### D2. `context_bundle` not in manifest — DEFERRED

`context_bundle` is deterministic from the transcript text and
`workflow_name`, both of which are already pinned in the manifest. Adding
it would duplicate information without enabling new replay capability.

### D3. `record_failure` doesn't verify eval-target linkage — DEFERRED

The seed is currently called only by tests inside this repo. Adding a
defensive cross-check would harden a not-yet-public boundary. Reason:
validate at real boundaries, which this isn't.

---

## Verdict

The duplication is gone; filename conventions live in one file; the
non-transcript empty-content path is closed. Run count: 152 tests pass.
