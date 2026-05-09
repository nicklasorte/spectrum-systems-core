# SSC-021 — Red Team Review

Document ID: SSC-021-REDTEAM
Scope: Failure persistence + human-review eval case format.
Reviewer goal: prove the slice does not blur product/learning boundaries
and does not let an unreviewed signal become required eval coverage.

Inputs reviewed:
- `docs/contracts/data_lake_contract.md` (sections 6, 6A, 8)
- `src/spectrum_systems_core/data_lake/paths.py`
- `src/spectrum_systems_core/data_lake/writer.py`
- `src/spectrum_systems_core/data_lake/failure_seed.py`
- `src/spectrum_systems_core/data_lake/__init__.py`
- `tests/test_failure_persistence.py`

Constitutional anchor (section 10):

```
failure -> failure_record -> eval_case_candidate
        -> reviewed eval_case -> regression suite
```

This slice closes the first three arrows and pins how the fourth arrow
will be authorized. The fourth arrow is intentionally not implemented.

---

## Findings

### M1 — must_fix — `is_eligible_for_regression` not symmetric with writer

**Observation.** `write_learning_artifact` accepts a `reviewed_eval_case`
with any of the three statuses. `is_eligible_for_regression` only returns
True for `accepted`. Good. But there is no test that pins the negative
case for `needs_revision` separately from `rejected`. A future refactor
could collapse them and silently let `needs_revision` slip through.

**Resolution status.** Resolved by
`test_needs_revision_candidate_does_not_become_required_eval` and
`test_accepted_candidate_is_eligible_for_regression`.

Status: must_fix → fixed (test added at write time).

### M2 — must_fix — Learning files could be confused with product files

**Observation.** The contract said "Only artifacts with `status ==
"promoted"` may be written under `processed/meetings/`." If we now write
`failure_record` etc. under `processed/meetings/<meeting_id>/...` without
clarifying scope, a casual reader may think section 6.1 is being violated.

**Resolution.** Section 6 now scopes its rule to "the top of the meeting
directory". Section 6A and the layout block both flag the dedicated
subdirectories (`failures/`, `eval_candidates/`, `reviewed_evals/`) as
non-product. Section 8 also lists them in the boundary rules.

Test that pins it:
`test_learning_artifacts_do_not_blur_into_product_artifact_dir` checks
that nothing other than `manifest__`, `debug__`, and promoted-product
files appears at the top of the meeting directory.

Status: must_fix → fixed.

### S1 — should_fix — Path traversal via artifact_id

**Observation.** `write_learning_artifact` uses `artifact.artifact_id`
verbatim as the filename. If a caller hand-builds an artifact with an
`artifact_id` containing `/` or `..`, the file lands outside the intended
directory.

**Resolution.** The writer now refuses any `artifact_id` that is empty or
contains `/` or `\`. Internal callers always go through `new_artifact`,
which assigns a UUID, so this is defense-in-depth at the boundary.

Status: should_fix → fixed (a covering test could be added; the
single-line check is straightforward).

### S2 — should_fix — `accepted` reviewed_eval_case shows envelope `evaluated`

**Observation.** A reader skimming the file may expect a status like
`accepted` or `promoted` on the envelope to mark "this is real". It is
deliberately `evaluated`, because acceptance is human-curatorial, not
control-decided.

**Resolution.** The fix doc records this as a deliberate choice; the
envelope status mirrors the constitution's allowed set
(`draft|evaluated|promoted|rejected`) and the human review status lives
on the payload. We do not add `accepted` to the envelope's allowed
statuses — that would muddy the control loop.

Status: should_fix → resolved by documentation.

### S3 — should_fix — `eval_case_candidate.review_status` vs reviewed payload `human_review_status`

**Observation.** The candidate payload uses `review_status`
("pending_human_review"), and the reviewed payload uses
`human_review_status` ("accepted"|"rejected"|"needs_revision"). The two
field names are similar enough to confuse a new engineer.

**Resolution.** The fix doc names this and chooses to keep the candidate
field as-is to avoid touching tests in `test_failure_seed.py` (no behavior
change). New engineers should rely on the artifact_type to disambiguate.

Status: should_fix → defer with reason.

### D1 — defer_with_reason — Reviewed eval cases do not auto-load into runner

The constitution explicitly defers regression-suite wiring to a later
slice. `is_eligible_for_regression` exists so a future loader can ask the
question and get a clear yes/no, but no loader is added here. This is by
design — the brief says "Do not wire reviewed eval cases into the
required eval runner yet."

### D2 — defer_with_reason — No revision history on a reviewed eval case

A `needs_revision` reviewed_eval_case does not record what was changed
when it is later re-reviewed. Today, a re-review writes a new
`reviewed_eval_case` artifact with a new `artifact_id`; the trail is the
collection of files in `reviewed_evals/`. A more formal history (e.g. a
JSONL log of review actions) is deferred until there is real review
volume. Cost of waiting is low; cost of guessing the schema now is high.

### D3 — defer_with_reason — `input_excerpt` defaults to empty

Reviewed eval cases include `input_excerpt`. The candidate produced by
`candidate_eval_case_from_failure` does not carry an excerpt today, so
the reviewed payload's `input_excerpt` defaults to `""`. A reviewer can
copy the relevant transcript snippet into `reviewer_notes`. Wiring an
auto-excerpt from the failure_record is a clear follow-up, but the
correct excerpt is a human judgment call (which line(s) to pin), so
defaulting to empty avoids encoding the wrong span.

---

## Trace exercise: can a new engineer follow failure → candidate → reviewed eval?

Walking the path:

1. A blocked pipeline run calls `record_failure(...)` →
   `failure_record` artifact (in memory).
2. `candidate_eval_case_from_failure(fr)` → `eval_case_candidate`
   (in memory).
3. `write_learning_artifact(lake_root, fr|cand|reviewed)` → file under
   the dedicated subdirectory.
4. A human (or a small UI later) reads the candidate file and calls
   `review_eval_candidate(cand, status, notes)` → `reviewed_eval_case`.
5. `write_learning_artifact(lake_root, reviewed)` → file under
   `reviewed_evals/`.
6. Only when `human_review_status == "accepted"` is the reviewed eval
   case eligible for inclusion in a future regression fixture, and even
   then the inclusion is a separate explicit step.

The contract document, the `failure_seed.py` docstring, and the test
file together pin this path. Naming is plain and matches the
constitution wording.

Verdict: a new engineer can follow it.

---

## Summary

| Tag | Severity | Status |
|-----|----------|--------|
| M1  | must_fix | fixed (tests pin both branches) |
| M2  | must_fix | fixed (contract scoped + dir-layout test) |
| S1  | should_fix | fixed (path-traversal guard) |
| S2  | should_fix | resolved (documentation) |
| S3  | should_fix | defer with reason (no behavior change) |
| D1  | defer_with_reason | regression-suite wiring is a later slice |
| D2  | defer_with_reason | review history out of scope |
| D3  | defer_with_reason | `input_excerpt` defaults to empty |
