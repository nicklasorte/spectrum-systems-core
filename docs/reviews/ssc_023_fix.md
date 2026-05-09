# Fix Pass — SSC-023

Document ID: SSC-023-FIX
Scope: must_fix and should_fix items raised by
`docs/reviews/ssc_023_redteam.md`. Closes the S2 follow-up from
`docs/reviews/ssc_usable_001_redteam.md`.

---

## M1. Per-field reason codes now surface to the index.

**Symptom**: the hardened eval emitted
`empty_required_field:agency` on the eval result, but the CLI only
forwarded `control_decision.reason_codes` (which carries
`failed:required_agency_question_summary_fields`) into the
`blocked_entries` list. The human reading the index could not tell
which field was empty without reading the manifest or debug report.

**Fix**:

- `data_lake/cli.py::process_meeting` now walks each pipeline result's
  `eval_results`, and for any failed eval, appends its `reason_codes`
  onto the blocked entry (de-duplicated against the control codes
  already there).
- `data_lake/markdown.py::_explain_reason` now recognizes the
  `empty_required_field:<field>` and `missing_field:<field>` prefixes,
  rendering them as
  `required field '<field>' was empty` and
  `required field '<field>' was missing`. Static codes
  (`failed:transcript_evidence`, `missing_required_evals`, …) keep
  their existing dictionary lookup.

**Test**:
`test_index_explains_empty_agency_in_plain_english` in
`tests/test_cli_process_meeting.py` asserts the index contains both the
machine-readable code (`empty_required_field:agency`) and the English
sentence (`required field 'agency' was empty`).

---

## S1. The regression-pinning test now asserts the corrected behavior.

**Symptom**: `test_process_meeting_runs_all_default_workflows` asserted
`agency_question_summary` was in `promoted_workflows` for
`m-golden-good`. That transcript has no `AGENCY:` line — the very
case PR #7's red-team flagged. Leaving the assertion would have masked
the fix.

**Fix**: the test now asserts `agency_question_summary` is in
`result.blocked_workflows`, with a comment pointing at SSC-023. A new
dedicated test
`test_agency_question_summary_blocks_when_agency_missing` is the
permanent fixture for this property — if someone weakens the eval
again, that test fails first.

**Test**: see above plus
`test_no_promoted_agency_question_summary_markdown_when_blocked` and
`test_no_promoted_agency_question_summary_json_when_blocked`, which
check that the boundary holds in both directions (no Markdown view, no
canonical JSON).

---

## S2. Scope held narrow.

**Symptom**: the task allowed widening the non-empty list to include
`decision_brief.recommendation` and `meeting_action_log.actions/open_count`
coupling. A broader sweep would have nudged this slice toward a schema
engine.

**Fix**: included `decision_brief.recommendation` (one line, consistent
with `agency_question_summary`'s rule). Deferred
`meeting_action_log.actions` (see D1 below). The full non-empty list is
now:

```python
NON_EMPTY_REQUIRED_FIELDS_BY_TYPE = {
    "agency_question_summary": ("agency", "question"),
    "decision_brief": ("recommendation",),
}
```

Three fields, two artifact types, zero new abstractions.

**Test**: `test_decision_brief_workflow_blocks_when_payload_invalid`
in `tests/test_decision_brief_workflow.py` already exercises the
empty-payload path and continues to pass. The valid `SAMPLE_BRIEF`
fixture has a non-empty `recommendation` and continues to promote.

---

## S3. `_is_empty_value` does not treat zero as empty.

**Symptom**: a naive `not value` truthiness check would have flagged
`open_count: 0` and `schema_version: 0` as empty. We do not currently
non-empty-check those fields, but a future addition could trip on it.

**Fix**: `_is_empty_value` returns True for `None`, blank/whitespace
strings, and zero-length collections only. Numbers and booleans are
"non-empty" by construction for this layer.

**Test**: indirectly covered by
`test_meeting_action_log_workflow_promotes_end_to_end` — the
SAMPLE_INPUT promotes with `open_count: 3` (non-zero), and adding a
non-empty rule for `open_count` later would still permit numeric
zero through; that is the desired property.

---

## D1. `meeting_action_log` non-empty rule deferred.

`actions` and `open_count` are tightly coupled by the extractor
(`open_count = len(actions)`). A coupled-field rule belongs in the
extractor or in the existing `content_signal` eval — not in the
required-fields layer, which is meant to be flat. Adding it would be
the first step toward a schema engine.

**Reason**: shape mismatch (coupled fields versus flat presence/non-empty
rules); existing `content_signal` already blocks empty action lists in
notes/summary sources, which is where the risk is real.

---

## D2. `meeting_minutes` non-empty rule deferred.

A meeting transcript with no listed `decisions` / `action_items` /
`open_questions` is already blocked by `transcript_evidence`
(transcript source) or `content_signal` (notes/summary source). A
third rule would be redundant.

**Reason**: there is no concrete failing case to point at; YAGNI.

---

## Verdict

All must_fix items resolved with code and tests. should_fix items
either resolved (S1, S3) or recorded with a specific reason (S2). The
two `defer_with_reason` items are explicitly tied to the constitution's
"don't build a schema engine" rule.

Test count: `tests/test_agency_question_summary_workflow.py` grew from
5 to 11; `tests/test_cli_process_meeting.py` grew from 15 to 19.
Baseline 167 → 177 tests, all green.
