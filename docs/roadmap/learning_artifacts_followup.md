# Learning Artifacts — Follow-up

Document ID: SSC-NEXT-MEMORY-037
Status: Roadmap, not implemented in this phase.

## Why this is a roadmap doc, not code

The Obsidian + harness-memory phase prompt (SSC-037) says:

> If learning artifacts exist on main, add Markdown views for
> `failure_record`, `eval_case_candidate`, `reviewed_eval_case`. If
> they do not exist on main, do not reimplement them here; instead
> create `docs/roadmap/learning_artifacts_followup.md`.

Section 2 of `docs/reviews/ssc_024_pr_drift_review.md` documents that
PR #4 (which would have persisted learning artifacts) is stale and
unmerged. The factory functions `record_failure(...)` and
`candidate_eval_case_from_failure(...)` are on main, but they only
produce in-memory `failure_record` and `eval_case_candidate` artifacts.
There is no on-disk persistence of any of the three types, so there
are no JSON files for Markdown views to render. This doc records the
gap so it is not lost.

## What still needs to be built

The constitution (§10) reserves the loop:

```
failure -> failure_record -> eval_case_candidate -> reviewed eval_case
        -> regression suite
```

To complete it, three pieces are needed:

1. **Persistence**. A `data_lake/writer.py`-shaped function that writes
   learning artifacts under
   `processed/meetings/<meeting_id>/failures/`,
   `eval_candidates/`, and `reviewed_evals/`. The writer must
   explicitly bypass the promotion rule that gates product artifacts,
   and the carve-out must be documented inline so it cannot be
   accidentally reused.
2. **Human review API**. A `review_eval_candidate(candidate, status,
   notes)` function (one of `accepted`, `rejected`,
   `needs_revision`) that produces a `reviewed_eval_case` artifact.
   The constitution and PR #4's draft both make clear that human
   review is a deliberate step; this function must not be called by
   any automated workflow.
3. **Markdown views**. Once 1 and 2 land, this phase's Markdown layer
   gains three more renderers — `failure_record.md`,
   `eval_case_candidate.md`, `reviewed_eval_case.md`. Each must:
   - sit next to the artifact JSON it views, under
     `markdown/artifacts/`,
   - declare `canonical: false` and `status: <artifact-status>` in
     frontmatter,
   - state in the body that "human edits to this Markdown do not
     change the canonical JSON,"
   - link back to the meeting index just like product artifacts.

## What this phase deliberately does NOT do

- It does not write learning artifacts to disk.
- It does not add `review_eval_candidate(...)`.
- It does not render Markdown for `failure_record`,
  `eval_case_candidate`, or `reviewed_eval_case`.

## Reuse guidance for whoever picks this up

PR #4 contains a workable persistence design. Treat it as a reference,
not a copy target — main has moved (PR #5 CI, PR #6 CLAUDE.md, PR #7
CLI/Markdown, PR #8 hardened required fields, and the SSC-NEXT-MEMORY
phase). In particular, `paths.py` already has a `markdown/`
subdirectory constant; the new learning-artifact subdirectories must
sit alongside it without colliding with the Markdown layout. The
contract (`data_lake_contract.md` §6.3) also now binds the Markdown
layer; any new §6A on learning artifacts must reference it so the
filename conventions are consistent.

## Why future workflows must read JSON, not Markdown, on review

If a future workflow needs to act on review decisions (for example,
to add an accepted candidate to the regression suite), it must read
the **canonical JSON** for `reviewed_eval_case`, not Markdown. This
preserves the boundary that core never reads Markdown back. A future
MCP integration that lets a human edit Markdown directly should write
its edits back as a NEW governed JSON artifact (the constitution's
"new envelope, not in-place mutation" rule), never silently update an
existing one.
