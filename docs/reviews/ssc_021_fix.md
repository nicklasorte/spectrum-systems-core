# SSC-021 — Fix Pass

Document ID: SSC-021-FIX
Scope: resolve red team findings from `ssc_021_redteam.md`.

Every `must_fix` and `should_fix` is closed. Every fix has a regression
test. Deferrals carry a one-line reason.

---

## Resolutions

### M1 — Eligibility symmetry pinned by tests

`is_eligible_for_regression` returns True only for
`human_review_status == "accepted"`. The negative branches are pinned by
two tests that cannot be deleted without making the file inconsistent:

- `test_rejected_candidate_does_not_become_required_eval`
- `test_needs_revision_candidate_does_not_become_required_eval`

A future refactor that collapses the statuses fails both tests.

### M2 — Product/learning boundary scoped in contract and pinned by test

Contract changes:

- Section 6 layout block now lists the three learning subdirectories.
- New section 6A pins paths, allowed `artifact_type` per directory,
  byte-determinism, and the rule that learning artifacts may be written
  even when product promotion is blocked.
- Section 8 (boundary rules) explicitly mentions learning subdirs.

Test that fails if learning files leak into the meeting's top level:
`test_learning_artifacts_do_not_blur_into_product_artifact_dir`.

### S1 — Path traversal guarded at the writer

`write_learning_artifact` now refuses any `artifact_id` that is empty or
contains `/` or `\`. Regression test:
`test_write_learning_artifact_rejects_unsafe_artifact_id`.

### S2 — Envelope status `evaluated` for accepted reviews is intentional

The constitution's `ALLOWED_STATUSES` is
`{draft, evaluated, promoted, rejected}`. We intentionally do not invent
a fifth envelope status for "accepted by reviewer" because acceptance is
not a control-decision outcome — it is a curatorial signal carried on
the payload (`human_review_status`). Adding `accepted` to the envelope
would create a second authority system, violating constitution §12
("Prefer one control model over many authority systems").

The `rejected` review case maps to envelope `rejected`, because that
matches the existing terminal-rejection meaning in the artifact model.

### S3 — Field name confusion `review_status` vs `human_review_status` — deferred

We keep `eval_case_candidate.review_status` and
`reviewed_eval_case.human_review_status` as separate field names. Reason:
the candidate field is also a contract with `test_failure_seed.py` that
already shipped, and the `artifact_type` reliably disambiguates the two
files. A renaming pass is appropriate later when there is real curator
volume; today it would burn more attention than it saves.

### Deferrals

| Tag | Reason |
|-----|--------|
| D1 | Regression-suite wiring is a separate explicit slice; brief says "Do not wire reviewed eval cases into the required eval runner yet." |
| D2 | Review history (re-review log) needs real review volume before its schema is worth committing. |
| D3 | `input_excerpt` defaults to empty because the right span is a human judgment call; auto-grabbing the wrong line is worse than asking the reviewer to fill it in. |

---

## Tests added or sharpened

- `test_failure_record_writes_to_failures_subdir`
- `test_eval_case_candidate_writes_to_eval_candidates_subdir`
- `test_reviewed_eval_case_accepted_writes_to_reviewed_evals_subdir`
- `test_reviewed_eval_case_rejected_writes_with_rejected_envelope`
- `test_reviewed_eval_case_needs_revision_payload`
- `test_write_learning_artifact_rejects_unknown_type`
- `test_write_learning_artifact_rejects_promoted_product_type`
- `test_write_learning_artifact_requires_meeting_id`
- `test_write_learning_artifact_routes_with_explicit_meeting_id`
- `test_write_learning_artifact_is_byte_deterministic`
- `test_write_learning_artifact_rejects_unsafe_artifact_id`
- `test_learning_subdirs_match_known_types`
- `test_review_eval_candidate_required_fields_present`
- `test_review_eval_candidate_invalid_status_fails`
- `test_review_eval_candidate_rejects_non_candidate`
- `test_review_eval_candidate_allowed_statuses_match`
- `test_rejected_candidate_does_not_become_required_eval`
- `test_needs_revision_candidate_does_not_become_required_eval`
- `test_accepted_candidate_is_eligible_for_regression`
- `test_golden_weak_seeds_failure_candidate_and_accepted_review`
- `test_learning_artifacts_do_not_blur_into_product_artifact_dir`
- `test_reviewed_eval_case_file_is_byte_identical_across_writes`

Total tests after this slice: **174 passed** (152 before + 22 new).

---

## Entropy check

Three short questions, three honest answers.

### Did this add any unnecessary module?

No. All code lives inside the existing `data_lake/` module:

- `paths.py` gained three subdirectory constants and three helpers.
- `writer.py` gained one new function `write_learning_artifact` and a
  small constant table mapping artifact_type → subdir.
- `failure_seed.py` gained `review_eval_candidate` and
  `is_eligible_for_regression`. The previous module docstring already
  predicted this slice, so the additions land where the file said they
  would.

No new top-level module. No new package. No new abstraction layer.

### Did this strengthen Produce → Evaluate → Decide → Promote?

Yes, on the Decide and Promote sides.

- A blocked Decide outcome can now be persisted as a
  `failure_record` and turned into an `eval_case_candidate` and then a
  human-reviewed `reviewed_eval_case` — closing the constitution's
  governed-learning loop on disk.
- Promote is unaffected: learning artifacts are written through a
  separate writer that is not allowed to write product artifact_types.
  The `status == "promoted"` rule for `processed/meetings/<meeting_id>/`
  top-level files is unchanged.

The control model is unchanged. No control function was modified.

### Can a new engineer understand the learning artifact path quickly?

Yes. The trace is:

1. `failure_seed.py` docstring lays out the four arrows.
2. `data_lake_contract.md` §6A lists the three paths and the rules.
3. `tests/test_failure_persistence.py::test_golden_weak_seeds_failure_candidate_and_accepted_review`
   is a single 30-line test that exercises the full chain end-to-end on
   the existing `m-golden-weak` fixture.
4. The on-disk layout is self-describing: `failures/<id>.json`,
   `eval_candidates/<id>.json`, `reviewed_evals/<id>.json`.

If the test passes, the chain works. If the chain works, the disk
layout matches the contract. No additional cognitive load was added.
