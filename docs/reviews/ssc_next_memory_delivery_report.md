# SSC-NEXT-MEMORY — Final Delivery Report

Document ID: SSC-NEXT-MEMORY-043
Branch: `claude/obsidian-harness-memory-X5831`

This phase added an Obsidian-friendly Markdown layout, harness-memory
JSONL projections, and debuggability upgrades. JSON stayed canonical;
no AI, agents, semantic search, vector DBs, or dashboards were added.

---

## Files changed

### New

- `src/spectrum_systems_core/data_lake/run_history.py`
- `src/spectrum_systems_core/data_lake/experience.py`
- `src/spectrum_systems_core/data_lake/eval_history.py`
- `tests/test_run_history.py`
- `tests/test_experience_history.py`
- `tests/test_eval_history.py`
- `tests/test_debug_report_upgrade.py`
- `tests/test_redteam_1_fixes.py`
- `tests/test_redteam_2_fixes.py`
- `tests/test_redteam_3_fixes.py`
- `docs/reviews/ssc_024_pr_drift_review.md`
- `docs/reviews/ssc_next_memory_redteam_1.md`
- `docs/reviews/ssc_next_memory_fix_1.md`
- `docs/reviews/ssc_next_memory_redteam_2.md`
- `docs/reviews/ssc_next_memory_fix_2.md`
- `docs/reviews/ssc_next_memory_redteam_3.md`
- `docs/reviews/ssc_next_memory_fix_3.md`
- `docs/reviews/ssc_next_memory_entropy_review.md`
- `docs/reviews/ssc_next_memory_delivery_report.md` (this)
- `docs/roadmap/learning_artifacts_followup.md`
- `docs/integrations/claude_mcp_obsidian.md`
- `docs/integrations/obsidian_dataview_examples.md`

### Modified

- `src/spectrum_systems_core/data_lake/markdown.py` — new vault
  layout, frontmatter hardening, backlinks, agency / topic / run
  rendering.
- `src/spectrum_systems_core/data_lake/cli.py` — wires the new layout
  and the harness-memory JSONL writers.
- `src/spectrum_systems_core/data_lake/debug.py` — added
  `failure_path` and `inspect_next` blocks; bumped schema_version 1→2.
- `src/spectrum_systems_core/data_lake/__init__.py` — exports new
  helpers.
- `tests/test_cli_process_meeting.py` — updated for the new layout
  and new frontmatter keys.
- `docs/contracts/data_lake_contract.md` — §6.3 rewritten to pin the
  full vault layout and frontmatter table; new §6.4 pins the
  harness-memory JSONL files.
- `README.md` — quickstart "Where outputs appear" updated; added a
  pointer to the integration docs.

---

## Roadmap slices completed

- SSC-024 — PR #4 drift review: documented, PR #4 deferred.
- SSC-025 — vault layout: implemented (`markdown/{index.md, artifacts,
  agencies, topics, runs}`).
- SSC-026 — frontmatter hardening: artifact md frontmatter now carries
  `artifact_id`, `content_hash`, `canonical_json_path`. Index md says
  `status: view` and `canonical: false`.
- SSC-027 — backlinks: artifact md links back to index, agency, topic,
  meeting wikilink, canonical JSON. Index links forward to artifact md
  and canonical JSON.
- SSC-028 — index upgrade: source paths, blocked-with-explanation,
  manifest / debug links, run records.
- SSC-029 — RT#1: `ssc_next_memory_redteam_1.md`.
- SSC-030 — fix #1: `ssc_next_memory_fix_1.md` (M1, M2, M3, S1, S2).
- SSC-031 — run history: `run_history.jsonl` + `runs/<run_id>.md`.
- SSC-032 — experience records: `experience_history.jsonl`.
- SSC-033 — eval score history: `eval_history.jsonl`.
- SSC-034 — debug upgrade: `failure_path` + `inspect_next`.
- SSC-035 — RT#2: `ssc_next_memory_redteam_2.md`.
- SSC-036 — fix #2: `ssc_next_memory_fix_2.md` (M4, M5, S3, S4).
- SSC-037 — learning artifacts followup: roadmap doc only (PR #4 not
  on main; persistence deferred).
- SSC-038 — Claude / MCP guide: `docs/integrations/claude_mcp_obsidian.md`.
- SSC-039 — Dataview examples:
  `docs/integrations/obsidian_dataview_examples.md`.
- SSC-040 — RT#3: `ssc_next_memory_redteam_3.md`.
- SSC-041 — fix #3: `ssc_next_memory_fix_3.md` (M6, M7, S5).
- SSC-042 — entropy audit: `ssc_next_memory_entropy_review.md`.
- SSC-043 — this report.

---

## Red team findings and fixes (summary)

Three review passes; three fix passes. Every must_fix and should_fix
landed with a regression test or a doc fix; every defer carries a
written reason.

| Pass | Findings | Resolved by |
| --- | --- | --- |
| RT#1 | M1 (canonical_json_path empty), M2 (boundary wording), M3 (index body wording), S1 (slug→string mapping), S2 (depth assumption) | fix #1 + 5 tests |
| RT#2 | M4 (overlap of run/experience), M5 (eval score type), S3 (inspection hints), S4 (jsonl in artifact index) | fix #2 + 6 tests |
| RT#3 | M6 (determinism wording), M7 (contract pin for view tokens), S5 (plugin lock-in note) | fix #3 + 3 tests |

Deferrals: D1 / D2 (cross-meeting indexes; PR-style learning views),
D3 / D4 (append-only run history; 2-minute timing claim), D5 / D6
(MCP sample config; vault-wide agency index).

---

## Tests added

| Test file | Test count |
| --- | --- |
| `test_run_history.py` | 8 |
| `test_experience_history.py` | 6 |
| `test_eval_history.py` | 5 |
| `test_debug_report_upgrade.py` | 5 |
| `test_redteam_1_fixes.py` | 5 |
| `test_redteam_2_fixes.py` | 6 |
| `test_redteam_3_fixes.py` | 3 |
| `test_cli_process_meeting.py` (extended) | +14 vs main |

Existing test suites covering the loop, control, evals, writer, and
golden transcripts continue to pass unchanged.

---

## Commands run

```bash
pip install -e ".[dev]" -q
python -m pytest
```

Result on the final commit: **226 passed**.

---

## Known deferrals

| ID | Topic | Why deferred |
| --- | --- | --- |
| RT#1 D1 | Cross-meeting agency index | per-meeting writer scope |
| RT#1 D2 | Markdown view of `manifest__` / `debug__` | redundant with run notes |
| RT#2 D3 | Append-only run history | breaks determinism |
| RT#2 D4 | Measured "2-minute" diagnose claim | structural defense suffices |
| RT#3 D5 | Sample MCP server config | no live MCP integration |
| RT#3 D6 | Vault-wide agency / topic indexes | reaffirms RT#1 D1 |
| SSC-037 | `failure_record` / `eval_case_candidate` / `reviewed_eval_case` Markdown views | learning persistence not on main; roadmap doc instead |
| SSC-024 | PR #4 — failure persistence + reviewed eval case format | stale; persistence not required for this phase |

---

## Constitution alignment

- **One loop**: Produce → Evaluate → Decide → Promote. Untouched.
  All additions are passive projections.
- **Top-level modules**: no new top-level modules. New files live
  under `data_lake/`.
- **Reserved names** (`failure_learning`, `ai_adapter`): not
  introduced. `data_lake/failure_seed.py` already on main is
  unchanged.
- **One artifact envelope**: respected. The Markdown layer's
  `meeting_index`, `agency_note`, `topic_note`, `run_note` are view
  shapes only; they never become envelope artifacts.
- **One control model**: respected.
- **No live model calls / agents / semantic search / vector DBs /
  dashboards**: confirmed.

---

## Top Engineer Practice alignment — Optimize for Debuggability

A new engineer can answer the phase's standard questions without
reading core source:

- **What input was processed?** `index.md` body (transcript path,
  metadata path, source_type, optional agency / topic).
- **What workflows ran?** `index.md` "Run records" + `run_history.jsonl`.
- **What artifacts promoted?** `index.md` "Promoted artifacts" with
  links to canonical JSON.
- **What blocked?** `index.md` "Blocked workflows" + per-workflow run
  notes + debug `failure_path`.
- **Why did it block?** Reason codes plus plain-English explanations
  in three places (index, run note, debug `inspect_next`).
- **What source text supports the output?** Artifact md "Source
  excerpts" section + canonical JSON `grounding`.
- **Where is the canonical JSON?** Frontmatter `canonical_json_path`
  on every artifact md; index links forward to JSON.
- **Where is the human-readable Markdown?** `markdown/` subtree under
  every meeting.

---

## Next recommended slice

Implement learning-artifact persistence and the `reviewed_eval_case`
shape — the third arrow of the constitution's governed-learning loop.
The roadmap is in `docs/roadmap/learning_artifacts_followup.md`. PR
#4 is a starting reference but should not be cherry-picked verbatim
because main has moved.

After that, a vault-wide agency / topic index (RT#1 D1, RT#3 D6) is
the next reasonable ergonomic step.

---

## PR body (also see PR description)

- **Summary**: Obsidian-friendly Markdown vault layout, harness-memory
  JSONL projections, debug upgrades, three RT/fix passes, two
  integration docs, contract update, README update, all tests pass.
- **Constitution alignment**: above.
- **Top Engineer Practice alignment**: above.
- **Obsidian / Markdown boundary**: documented in
  `docs/integrations/claude_mcp_obsidian.md`; pinned in
  `docs/contracts/data_lake_contract.md` §6.3 / §6.4.
- **Harness memory summary**: three JSONL files
  (`run_history.jsonl`, `experience_history.jsonl`,
  `eval_history.jsonl`) plus per-run Markdown notes; passive,
  deterministic, non-authoritative.
- **Red team reviews**: 3 (RT#1 / RT#2 / RT#3).
- **Fix passes**: 3, every must_fix has a regression test.
- **Tests**: 226 passing.
- **Deferrals**: listed above.
