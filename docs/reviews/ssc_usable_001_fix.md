# Fix Pass — SSC-USABLE-001

Document ID: SSC-USABLE-001-FIX
Scope: must_fix and should_fix items raised by
`docs/reviews/ssc_usable_001_redteam.md`.

---

## M1. Markdown view layer is now in the data lake contract.

**Symptom**: the contract listed three file kinds under
`processed/meetings/<meeting_id>/` (artifact JSON, manifest, debug). The
new `markdown/` subdirectory was a fourth kind but was not described,
inviting the assumption that anything under that directory is a product
artifact.

**Fix**: `docs/contracts/data_lake_contract.md` now contains §6.3
"Markdown views" stating:

- Markdown is regenerated from canonical JSON.
- Core never reads Markdown as input.
- Markdown is not a product artifact and is not subject to the promotion
  gate (the promotion gate applies to JSON artifacts only).
- Filenames: `<artifact_type>.md` per artifact, `index.md` per meeting.
- Every Markdown file begins with YAML frontmatter containing the
  required keys.

**Test**: `test_promoted_json_artifact_unchanged_by_markdown_step` and
`test_promoted_json_files_are_not_under_markdown_dir` in
`tests/test_cli_process_meeting.py` defend the source-of-truth boundary.

---

## M2. Blocked workflows now render plain-English explanations.

**Symptom**: a transcript with only `DECISION:`/`ACTION:`/`QUESTION:`
lines blocks `decision_brief` with the bare reason code
`failed:transcript_evidence`. A non-engineer reader cannot tell the
difference between "real failure" and "no signal for this type of
artifact in this transcript."

**Fix**: `data_lake/markdown.py::_BLOCK_EXPLANATIONS` maps each
expected reason code to one short English sentence. The index renders
both the code (for engineers and tooling) and the explanation (for
humans):

```
- **decision_brief**: failed:transcript_evidence (no signal for this
  artifact type in the transcript)
```

The fail-closed control path is unchanged.

**Test**: `test_index_explains_block_reasons_in_plain_english` in
`tests/test_cli_process_meeting.py`.

---

## S1. Index frontmatter `trace_id` is now meaningful.

**Symptom**: the index frontmatter wrote `trace_id: ""` because the
index spans multiple workflows, each with its own trace_id. An empty
field is worse than no field for vault filtering.

**Fix**: `data_lake/markdown.py::_index_trace_id(meeting_id)` emits a
deterministic `meeting-<meeting_id>` token. The per-workflow trace_ids
also appear in the body of the index next to each artifact link, so the
mapping from a meeting to its individual run trace_ids is preserved.

**Test**: `test_index_trace_id_is_meaningful_meeting_token` in
`tests/test_cli_process_meeting.py`.

---

## S2. Empty `agency` on `agency_question_summary` — recorded as follow-up.

The required-fields eval for `agency_question_summary` accepts an empty
string for `agency`. This was visible before the CLI; the CLI now
exposes it to humans via Markdown. The right fix is in `evals/runner.py`
(grow a non-empty-string check), not in the CLI. Logged here so it is
not lost; intentionally out of scope for this slice.

---

## S3. Single transcript load, threaded through every workflow.

**Symptom**: a re-read of `cli.py` confirmed the implementation already
loads the transcript once via `load_meeting` and threads the resulting
`TranscriptInput` through each `run_transcript_pipeline` call. No code
change required.

**Test**: `test_process_meeting_runs_all_default_workflows` indirectly
exercises the four-workflow path; the manifest of each pipeline result
shares the same `input_transcript_hash` and `input_metadata_hash`
because they came from the same loaded `TranscriptInput`.

---

## Verdict

All must_fix items resolved with code, contract amendment, and test
coverage. should_fix items either resolved (S1) or recorded with a
specific reason (S2 belongs to evals, not the CLI; S3 was a false
alarm). Test count: `test_cli_process_meeting.py` grew from 13 to 15.
