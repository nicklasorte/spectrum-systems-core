# SSC-NEXT-MEMORY — Fix Pass #3

Resolves the must_fix and should_fix items from
`docs/reviews/ssc_next_memory_redteam_3.md`.

| ID  | Status | Where the fix landed | Regression test |
| --- | ------ | -------------------- | --------------- |
| M6  | fixed  | `docs/integrations/claude_mcp_obsidian.md` now says "byte-deterministic projections of JSON: identical inputs produce a byte-identical Markdown file across runs (constitution §10; data-lake contract §9)." | n/a (doc fix) |
| M7  | fixed  | `docs/contracts/data_lake_contract.md` §6.3 now enumerates every Markdown `artifact_type` token (`meeting_index`, `agency_note`, `topic_note`, `run_note`) plus the per-kind required frontmatter keys, and adds a new §6.4 pinning the harness-memory JSONL files. | `tests/test_redteam_3_fixes.py::test_M7_contract_pinned_artifact_type_tokens_are_emitted`, `test_M7_index_status_is_view_not_promoted`, `test_M7_runs_md_carries_run_note_and_decision_field` |
| S5  | fixed  | `docs/integrations/obsidian_dataview_examples.md` now flags each Dataview-specific snippet inline. | n/a (doc fix) |
| D5  | deferred | No live MCP server is implemented in this repo; a sample MCP config would have to specify a client we have not tested. | n/a |
| D6  | deferred | Cross-meeting "all FCC artifacts ever" page would expand the per-meeting writer's scope. Reaffirms RT#1's D1. | n/a |
