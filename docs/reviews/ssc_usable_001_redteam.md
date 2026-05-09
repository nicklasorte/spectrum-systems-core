# Red Team Review — SSC-USABLE-001

Document ID: SSC-USABLE-001-REDTEAM
Scope: One-command meeting processing CLI and Markdown views.

---

## Method

Re-read the new code with four questions:

1. Is this easy for a human to use?
2. Can a new engineer run it from the README?
3. Does Markdown blur the source-of-truth boundary?
4. Did we add unnecessary architecture?

---

## Findings

### must_fix

**M1. Markdown lives under `processed/meetings/<meeting_id>/`, which the
data lake contract reserves for promoted JSON artifacts and run
metadata.**
The contract (`docs/contracts/data_lake_contract.md` §6) lists exactly
three kinds of files under `processed/meetings/<meeting_id>/`: promoted
artifact JSONs (`<artifact_type>__<slug>.json`), manifests
(`manifest__<run_id>.json`), and debug reports (`debug__<run_id>.json`).
Markdown views are a fourth kind. The `markdown/` subdirectory keeps
them visually distinct, but a contract that doesn't mention them invites
a future reader to assume any file in `processed/meetings/<meeting_id>/`
is governed by the promotion gate.
*Fix*: amend the contract to call out the `markdown/` subdirectory as a
view layer, with explicit rules: (a) Markdown is regenerated from
canonical JSON; (b) Markdown is never read as input by core; (c)
Markdown is not a product artifact and is not subject to the promotion
gate. Without this clarification the contract and the code drift apart.

**M2. The CLI runs every default workflow against any transcript, even
when the transcript has no signal for some of them.**
For a transcript that contains only `DECISION:`/`ACTION:`/`QUESTION:`
lines, the `decision_brief` workflow runs to completion and is blocked
by `transcript_evidence`. That is correct behavior, but the index
Markdown lists `decision_brief` under "Blocked workflows" with the
reason code `failed:transcript_evidence` — which a non-engineer reader
will not understand. The block looks like an error rather than "this
artifact type doesn't apply to this transcript."
*Fix*: keep the block (fail-closed is the constitution rule) but render
the index entry with a one-line plain-English explanation alongside the
reason code, so a human can tell apart "real failure" from "no signal
for this artifact type."

### should_fix

**S1. The index Markdown's `trace_id` frontmatter is empty.**
The index spans multiple workflows, each with its own `trace_id`. The
current rendering writes `trace_id: ""`. An Obsidian user filtering by
`trace_id` would get an empty value for every meeting index, which is
worse than not having the field at all. The field exists because the
spec required it, but it is misleading in this form.
*Fix*: either (a) emit a list of `trace_ids` keyed by workflow under a
new `trace_ids` map in the frontmatter, or (b) drop `trace_id` from the
index frontmatter (still required on per-artifact frontmatter, where it
is meaningful). Document the choice.

**S2. The `agency_question_summary` workflow promotes on the
`m-golden-good` transcript even though the transcript has no `AGENCY:`
line.**
Inspecting the smoke output: the `agency` field is the empty string and
the `citations` list is empty, but the artifact promotes because the
required-fields eval treats "field is present and is a string" as
passing for `agency`, and because the `QUESTION:` lines provide
grounding. The Markdown view dutifully renders `**Agency:**
_(unspecified)_`, which is honest but exposes that the artifact has
nearly no content. This isn't introduced by the CLI — it's a pre-existing
gap in `evals/runner.py::REQUIRED_FIELDS_BY_TYPE` — but the CLI now
makes the gap visible to humans for the first time.
*Fix*: out of scope for this CLI slice; record as a follow-up so the
required-fields eval can grow a non-empty-string check for fields like
`agency`. Linked here so it is not lost.

**S3. The `process_meeting` function risks re-loading the transcript
once per workflow.**
On a careful re-read, the implementation already loads the transcript
once via `load_meeting` and threads the resulting `TranscriptInput`
through each `run_transcript_pipeline` call (`cli.py` lines 67–87). No
fix needed; flagged here so future readers understand why the CLI takes
this shape and don't accidentally regress it.

### defer_with_reason

**D1. No `process-meeting --dry-run` flag.**
A reader might want to preview which workflows will promote without
writing anything. The pipeline supports `write_outputs=False`, but the
CLI does not expose it. Reason: the goal of this slice is "one command
that does the obvious thing." Adding flags before there is a real use
case is the architecture-creep this review is meant to prevent.

**D2. No `process-all-meetings` subcommand.**
The CLI processes one meeting per invocation. A shell loop over
`raw/meetings/*` is the right answer for now. Reason: shell pipelines
already do this; baking it into the CLI would invite progress-bar UX,
parallelism flags, and other scope expansion that does not serve the
core loop.

**D3. Markdown is not validated for "Obsidian-friendly" beyond the
frontmatter shape.**
We assert frontmatter keys exist and that links are present. We do not
assert that the Markdown renders cleanly in Obsidian, that wikilinks
work across vaults, or that frontmatter values lint as YAML 1.2. Reason:
testing third-party renderer behavior is not a trust property of this
system; Markdown is a view, not the canonical artifact.

---

## Loop integrity check

- The CLI does not introduce a new control path. Every promotion still
  goes through `promotion/promoter.py::promote_if_allowed`, called by
  `pipeline.run_transcript_pipeline`. Markdown rendering happens after
  promotion and reads the promoted artifact's payload only.
- No new module names introduced. The CLI lives under `data_lake/`
  because its job is to read from and write to the lake.
- No AI, no agents, no embeddings, no semantic search, no remote
  persistence. Pure deterministic Python.
- JSON remains the canonical artifact: the `process_meeting` test
  `test_promoted_json_artifact_unchanged_by_markdown_step` proves the
  JSON bytes are untouched by the Markdown step.

---

## Verdict

Two must_fix items (contract amendment, index reason-code phrasing) and
three should_fix items (index `trace_id` frontmatter, agency-empty
gap noted as follow-up, transcript reloading). No new ceremony detected;
the CLI is one file plus one renderer file.
