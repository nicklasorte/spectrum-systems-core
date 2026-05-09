# Red Team Review — SSC-023

Document ID: SSC-023-REDTEAM
Scope: Non-empty required-field eval hardening. Closes the S2 follow-up
recorded in `docs/reviews/ssc_usable_001_redteam.md`.

---

## Method

Re-read the diff with four questions:

1. Does this fix the actual weakness from PR #7?
2. Did it overgeneralize into schema machinery?
3. Are the reason codes understandable to a new engineer?
4. Does it preserve "JSON is the source of truth, Markdown is a view"?

---

## Weakness recap

PR #7's red-team noted that `agency_question_summary` could promote
against the `m-golden-good` transcript even when the transcript had no
`AGENCY:` line. The required-fields eval treated `agency: ""` as
"present, therefore fine." Markdown rendered `**Agency:** _(unspecified)_`
— honest but the artifact was a lie, and it had already been promoted.

The fix layer is `evals/runner.py`. Adding a non-empty check there closes
the gap without touching the loop, the control function, or the
promotion path.

---

## Findings

### must_fix

**M1. The CLI's blocked-entries dict only carried control-level reason
codes, so the empty-field detail (`empty_required_field:agency`) was
invisible to the human reading the index.**
The hardened eval correctly attaches per-field codes to the eval result,
but the cli previously copied only `control_decision.reason_codes` into
`blocked_entries`. The index would have shown
`failed:required_agency_question_summary_fields` with a generic
explanation — adequate for engineers, useless for humans.
*Fix*: `data_lake/cli.py` now appends each failed eval's reason codes
(de-duplicated) onto the blocked entry, and `data_lake/markdown.py`
gained prefix-aware explanations for `empty_required_field:<field>` and
`missing_field:<field>`. The index now reads:

```
- **agency_question_summary**: failed:required_agency_question_summary_fields,
  empty_required_field:agency, empty_required_field:question (a required
  field was missing or empty in the artifact's required-fields eval;
  required field 'agency' was empty; required field 'question' was empty)
```

### should_fix

**S1. `test_process_meeting_runs_all_default_workflows` previously
asserted the buggy outcome (`agency_question_summary` in
`promoted_workflows` for `m-golden-good`).**
This test pinned the exact behavior PR #7's red-team flagged as wrong.
Leaving it would have masked the regression we are intentionally
introducing.
*Fix*: the test now asserts that `agency_question_summary` is in
`blocked_workflows` for `m-golden-good`, with a comment pointing back to
SSC-023. The new dedicated test
`test_agency_question_summary_blocks_when_agency_missing` is the
permanent fixture for this property.

**S2. The "should be non-empty" list could have grown unchecked.**
The task hinted that `decision_brief.recommendation` could also be
non-empty, and that `meeting_action_log.actions` and `open_count` could
be coupled. A broader sweep would creep toward a schema engine.
*Decision*: include only the two fields named explicitly
(`agency_question_summary.agency`, `agency_question_summary.question`)
plus `decision_brief.recommendation` for consistency. Skip
`meeting_action_log` for now — `actions` and `open_count` covary by
construction (`open_count = len(actions)`), so a pure non-empty rule is
not the right shape, and the existing `content_signal` eval already
blocks empty-content runs from non-transcript sources. Logged as
`defer_with_reason` D1 below.

**S3. `_is_empty_value` deliberately does not treat numbers as empty.**
A naive `not value` would have flagged `open_count: 0` and
`schema_version: 0` as empty. The helper restricts emptiness to None,
blank/whitespace strings, and zero-length collections — every other
type is "non-empty by construction" for this layer.
*No fix needed*; flagged so future readers don't add `if not value:`
shortcuts.

### defer_with_reason

**D1. `meeting_action_log` does not get a non-empty rule.**
The task allowed conditional logic ("`actions` non-empty when
`open_count > 0` or vice versa"). That conditional belongs in the
extractor (where `open_count = len(actions)` makes the two impossible to
disagree) or in `content_signal` (which already covers the
notes/summary cases). Adding a coupled-fields rule to
`_check_required_fields` would be the first step toward a schema engine
— exactly what the constitution and this task warn against.

**D2. `meeting_minutes` does not get a non-empty rule.**
A meeting with a transcript but no listed `decisions` /
`action_items` / `open_questions` is currently caught by
`transcript_evidence` (transcript-source) or `content_signal`
(notes/summary source). Layering a third rule would be redundant and
would change the semantics of "valid minutes" without a concrete
failing case to point at.

**D3. The text shown in the index is good for an engineer-leaning
reader, less good for a non-technical one.**
"required field 'agency' was empty" is plain English, but the raw
reason code is still printed alongside it (e.g.
`empty_required_field:agency`). This matches the prior pattern from
SSC-USABLE-001 fix M2 where both the code and the explanation are
shown. Reason: tooling and grep need the code, humans need the
sentence; the cost of repeating both is low.

---

## Loop integrity check

- The new check lives entirely in `evals/runner.py`. The loop, control
  function, and promotion path are untouched.
- No new module names introduced. No AI, no agents, no embeddings, no
  semantic search, no dashboards.
- No CLI flag changes. The CLI's only change is to copy already-existing
  per-eval reason codes into the blocked-entry dict it already builds.
- JSON remains the canonical artifact: when the new check blocks
  promotion, `write_promoted_artifact` is never called and
  `write_artifact_markdown` is gated on `result.promoted` — the test
  `test_no_promoted_agency_question_summary_json_when_blocked` defends
  this property.

---

## Verdict

One must_fix item (CLI must surface per-field codes), two should_fix
items (regression-pinning test updated, scope kept narrow). No new
ceremony detected; the change is two new eval entries, one helper, and
two prefix-aware view strings.
