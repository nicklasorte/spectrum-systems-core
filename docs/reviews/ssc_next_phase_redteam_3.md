# Red Team Review #3 — Goldens, Evals, Failure-to-Eval, Complexity Creep

Document ID: SSC-REDTEAM-003
Scope: SSC-015 golden suite, SSC-016 failure-to-eval seed, end-to-end eval
discipline, and the question "are we adding ceremony or behavior?".

---

## Method

Re-read the new and changed code with one question: would a new engineer
be able to explain a failed run quickly, and would they be tempted to
delete anything? Compare the test bodies to the constitution's
"Tests must prove useful trust properties" rule. Look for duplication and
architecture creep that shows up only after several slices.

---

## Findings

### must_fix

**M1. `data_lake/extract.py` has four near-duplicate grounders.**
`_ground_meeting_minutes`, `_ground_decision_brief`,
`_ground_agency_question_summary`, and `_ground_meeting_action_log` are
the same loop with different `(prefix, kind)` tables. The body — split
on `:`, build a span dict, append — is identical in all four. This is
exactly the "duplicate functions" condition the entropy review (SSC-019)
exists to remove. Fix it now while the structure is fresh.
*Fix*: replace the four functions with one config table mapping
`workflow_name -> [(prefix, kind), ...]` and a single
`_ground_by_prefix_table` function. The exposed `GROUNDED_EXTRACTORS`
dict stays intact so `pipeline.py` is unchanged.

### should_fix

**S1. Manifest and debug filename patterns are inlined in `pipeline.py`.**
`pipeline.py` builds `manifest__<run_id>.json` and `debug__<run_id>.json`
by string concatenation. The pattern is also referenced indirectly in
`index.py` (skip filenames starting with `manifest__` or `debug__`). Two
sites, no central definition. A future rename has two places to forget.
*Fix*: add `manifest_filename(run_id)` and `debug_filename(run_id)` (and
their reverse skip rule) to `paths.py` so `pipeline.py` and `index.py`
both call them.

**S2. Non-transcript source types can promote empty-content artifacts.**
The `transcript_evidence` eval only runs for `source_type == "transcript"`.
A `notes` or `summary` source whose extractor produces a payload with
all empty lists — only `meeting_id`, `title`, `grounding=[]` — passes
`non_empty_payload` (the dict has non-empty values for those keys) and
all required-field evals. The artifact promotes with no real content.
This is probably surprising for the new engineer reading the debug
report.
*Fix*: leave the promotion path alone (notes-only artifacts may be valid
when intentional), but surface a `content_signal` eval that fails for
`source_type` in `{"notes", "summary"}` when every content list in the
payload is empty. Block on it, with a clear reason code.

**S3. Golden suite covers only `meeting_minutes`.**
`m-golden-good`, `m-golden-malformed`, and `m-golden-weak` all run the
`meeting_minutes` workflow. The other three workflows
(`decision_brief`, `agency_question_summary`, `meeting_action_log`)
have no golden coverage. A bug specific to one of them would not show up
in the golden suite.
*Fix*: add one valid golden fixture for `agency_question_summary` (the
most product-distinct of the four).

### defer_with_reason

**D1. Failure-record persistence to disk.**
The seed builds `failure_record` and `eval_case_candidate` artifacts but
does not write them. The constitution explicitly defers the human-review
step. Persisting them now would invent a layout that the human-review
flow may not want. Reason: defer until the human-review path is
designed.

**D2. Manifest does not list `context_bundle`.**
`context_bundle` is deterministic from `transcript_text` and the
`workflow_name`. Listing it in the manifest would duplicate information
already implied by `input_transcript_hash`. Reason: include in manifest
only the things needed to reconstruct or verify a run, not everything
the run produced.

**D3. `failure_seed.record_failure` does not verify that eval results
target the supplied artifact.**
A misuse could record an unrelated eval as a failure of this artifact.
Adding the cross-check costs a few lines and a custom error path. The
seed is internal and only invoked from tests today. Reason: validate at
real boundaries; this isn't one yet.

---

## Loop integrity check

- Tests prove behavior: golden tests pin specific extracted decisions,
  filename-byte-equality, and reason codes. None are pure ceremony.
- The failure-to-eval loop is one short hop (failure → record → candidate)
  with a hard "no" on auto-promotion, matching the constitution.
- Complexity creep: `data_lake/` now has 12 files; M1 will collapse one
  of them (`extract.py`) by ~50 lines. The entropy review (SSC-019) will
  decide whether the rest of the layout is justified.

---

## Verdict

One must_fix (extract.py duplication) and three should_fix (filename
helpers, content_signal eval, second-workflow golden). No new ceremony
detected. The failure-to-eval seed is appropriately small.
