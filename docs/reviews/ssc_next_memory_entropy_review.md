# SSC-NEXT-MEMORY — Complexity / Entropy Audit

Document ID: SSC-NEXT-MEMORY-042
Status: Audit. Should drive deletion or merge of any addition that
fails the constitutional questions.

The constitution's three questions are answered explicitly for every
addition in this phase.

---

## What was added

### Code

| File | Purpose |
| --- | --- |
| `src/spectrum_systems_core/data_lake/markdown.py` (rewritten) | Vault layout, frontmatter hardening, backlinks, agency / topic notes, index upgrade |
| `src/spectrum_systems_core/data_lake/run_history.py` | Run history JSONL + per-run Markdown note |
| `src/spectrum_systems_core/data_lake/experience.py` | Harness experience JSONL |
| `src/spectrum_systems_core/data_lake/eval_history.py` | Eval score JSONL |
| `src/spectrum_systems_core/data_lake/debug.py` (extended) | `failure_path` + `inspect_next` blocks |
| `src/spectrum_systems_core/data_lake/cli.py` (rewired) | Wires the new layout into `process-meeting` |

### Docs

| File | Purpose |
| --- | --- |
| `docs/reviews/ssc_024_pr_drift_review.md` | PR #4 drift assessment |
| `docs/reviews/ssc_next_memory_redteam_{1,2,3}.md` | Three red team passes |
| `docs/reviews/ssc_next_memory_fix_{1,2,3}.md` | Their fix passes |
| `docs/reviews/ssc_next_memory_entropy_review.md` (this) | Entropy audit |
| `docs/roadmap/learning_artifacts_followup.md` | SSC-037 deferral |
| `docs/integrations/claude_mcp_obsidian.md` | MCP / Claude boundary guidance |
| `docs/integrations/obsidian_dataview_examples.md` | Optional Dataview examples |
| `docs/contracts/data_lake_contract.md` (§6.3 rewritten + §6.4 added) | Contract pin for new layout / harness memory |

### Tests (new files)

`test_run_history.py`, `test_experience_history.py`,
`test_eval_history.py`, `test_debug_report_upgrade.py`,
`test_redteam_1_fixes.py`, `test_redteam_2_fixes.py`,
`test_redteam_3_fixes.py`. Existing `test_cli_process_meeting.py` was
updated for the new layout.

---

## Q1 — Did every addition prevent a failure or improve a measurable signal?

| Addition | Failure prevented or signal improved |
| --- | --- |
| Layout migration (artifacts/agencies/topics/runs subdirs) | Markdown views were going flat; a real vault would crowd. The hierarchy makes Obsidian and `cat`/grep usable. |
| `canonical_json_path` in frontmatter | Prevented "I have an artifact MD, where is the JSON?" debugging detour. |
| Backlinks block + index card | Prevented "I'm in an artifact MD, how do I get back to the meeting?" detour. |
| `run_history.jsonl` | Prevented "what ran on this meeting?" detour by exposing manifest/debug paths in one file. |
| `experience_history.jsonl` | Prevented "what happened?" question (input_hash, output_hash, summary). Distinguishable from run_history (RT#2 M4). |
| `eval_history.jsonl` | Prevented "which evals failed where?" detour. |
| Debug `failure_path` + `inspect_next` | Prevented "I see a block, what do I check next?" detour. |
| Agency / topic notes | Prevented "which meetings touched FCC?" detour at the per-meeting level. (Cross-meeting deferred per RT#1 D1.) |
| Contract §6.3 / §6.4 expansion | Locked the new vocabulary so future renames cannot break Dataview queries silently. |

Pass.

## Q2 — Did every addition strengthen Produce → Evaluate → Decide → Promote?

The harness memory is **side-channel**. It does not produce, evaluate,
decide, or promote. That is precisely the design.

The only place where the loop's behavior could be affected is
`debug.py::_build_inspect_next` — but that is a JSON debug field, not
a control input. The control function (`control/decision.py`) has not
been touched in this phase. The promotion gate
(`promotion/promoter.py`) has not been touched. The eval runner
(`evals/runner.py`) has not been touched.

Pass.

## Q3 — Can failures be explained quickly by a new engineer?

Three independent surfaces explain a blocked workflow today:

1. `index.md` — lists each blocked workflow with reason codes and a
   plain-English explanation.
2. `runs/<run_id>.md` — per-run note repeating the explanation and
   linking to manifest / debug JSON.
3. `debug__<run_id>.json` — `failure_path` block and `inspect_next`
   list.

Plus the JSONL projections (`run_history`, `eval_history`,
`experience_history`) for batch / scripted inspection.

A new engineer who finds the meeting directory can answer "what
blocked, why, and where do I look?" without reading core source.

Pass.

---

## Q4 — Did we add redundant memory files?

`run_history.jsonl` and `experience_history.jsonl` initially looked
overlapping (RT#2 M4). The fix made the distinction load-bearing:

- `run_history` carries pointers (`manifest_path`, `debug_path`,
  `run_markdown_path`); regression test asserts no lesson fields.
- `experience_history` carries the lesson (`input_hash`,
  `output_hash`, `human_readable_summary`); regression test asserts no
  pointer fields.

Either removed in isolation would lose a real surface. Kept.

`eval_history.jsonl` is a third file. It is a per-eval projection, not
a per-run projection, so its row shape and sort key differ. Reading
"which evals failed across all workflows on this meeting" is awkward
inside `run_history` (one record per workflow run, not per eval).
Kept.

## Q5 — Did we add indexes that are not yet useful?

No new top-level index. The artifact_index.jsonl is unchanged. The
new meeting-level JSONL files (`run_history`, etc.) are useful at the
per-meeting scope they ship at.

Per-agency and per-topic Markdown notes are per-meeting. A vault-wide
agency index was deferred (RT#1 D1) until vault use shows the need.
Defer kept.

## Q6 — Should anything be removed, merged, or deferred?

- Remove: nothing.
- Merge: `run_history` and `experience_history` were considered for
  merge in RT#2 M4 and explicitly kept after the fix made the
  distinction enforceable.
- Defer: cross-meeting agency / topic indexes (RT#1 D1, RT#3 D6),
  learning-artifact persistence (SSC-037 → roadmap), live MCP
  integration (RT#3 D5).

---

## Constitutional question summary

- **Does this serve Produce → Evaluate → Decide → Promote?** Yes,
  *passively*. Harness memory + debuggability surfaces feed the
  human reading the loop, not the loop itself.
- **Did we kill complexity early?** Yes — every red-team pass
  surfaced an entropy concern (overlap, ambiguity, contract drift)
  and the fix pass closed it with a regression test.
- **Did we build fewer, stronger loops?** No new loops were built.
  The only loop is still `run_governed_loop` in
  `workflows/_loop.py`. The new files are projections.

Phase passes the audit.
