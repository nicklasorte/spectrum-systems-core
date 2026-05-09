# Claude + MCP + Obsidian — Integration Guide (no live MCP)

Document ID: SSC-NEXT-MEMORY-038
Status: Guidance only. No MCP server or live model integration is
implemented in this repo; this document defines the boundary so a
future integration cannot violate the constitution by accident.

This guide complements `docs/architecture/system_constitution.md` and
`docs/contracts/data_lake_contract.md` (§6.3). Where they conflict,
they win.

---

## Boundary at a glance

| Layer | Role | Authority |
| --- | --- | --- |
| Raw transcript / metadata | input  | source-of-truth INPUT |
| `processed/<...>/<artifact_type>__<slug>.json` | promoted artifact | **canonical OUTPUT** |
| `processed/<...>/markdown/...` | regenerated views | not authoritative |
| Obsidian vault on top of `markdown/` | human interface | not authoritative |
| Claude reading the vault via MCP | reading agent | may read, must cite JSON |

Two rules drive everything below:

1. **JSON is canonical. Markdown is a regenerated view.**
2. **Core never reads Markdown back into the loop.**

Both rules survive even when Claude is added.

---

## Claude / MCP — what is allowed

### 1. Read access to Markdown views

A Claude assistant connected through MCP may read everything under
`processed/meetings/<meeting_id>/markdown/`:

- `index.md` — meeting summary
- `artifacts/<artifact_type>.md` — view of one promoted artifact
- `agencies/<slug>.md`, `topics/<slug>.md` — per-meeting cross-cuts
- `runs/<run_id>.md` — per-run view of harness memory

Reading these files is fine. They were written for human consumption
and the frontmatter explicitly says `canonical: false` (or carries a
`canonical_json_path` pointing to the JSON source-of-truth).

### 2. Citing canonical JSON when making claims

When Claude makes a factual claim derived from a meeting, it should
cite the **canonical JSON path** that supports it, not the Markdown
file. Every artifact Markdown frontmatter contains
`canonical_json_path:`. Use it.

Citation example:

> The meeting promoted a decision_brief artifact recommending X
> (source: `processed/meetings/m-q3-planning/decision_brief__abc.json`).

### 3. Preferring promoted artifacts over learning / debug artifacts

Promoted artifacts (`status: promoted`) are the only artifacts that
have been gated by the control function. Debug reports
(`debug__<run_id>.json`) and harness-memory JSONL files
(`run_history.jsonl`, `experience_history.jsonl`,
`eval_history.jsonl`) are run records, not products. Claude may use
them to **diagnose** a meeting, but should not present their contents
as confirmed facts about the meeting.

If learning artifacts (`failure_record`, `eval_case_candidate`,
`reviewed_eval_case`) ever land in this repo (see
`docs/roadmap/learning_artifacts_followup.md`), the same rule
applies: only `reviewed_eval_case` artifacts that a human reviewed
through the governed flow are authoritative.

---

## Claude / MCP — what is NOT allowed

### 1. Treating Markdown edits as canonical

If a user edits an artifact Markdown file in Obsidian, the change is
local cosmetic markup. The next `spectrum-core process-meeting` run
will overwrite the file. Claude must **never**:

- summarize a Markdown file and treat it as the artifact,
- stage a Markdown file as the canonical artifact,
- propose to merge a Markdown change as if it were an artifact
  change.

### 2. Editing generated Markdown directly

Generated Markdown files (`index.md`, `artifacts/...md`,
`agencies/...md`, `topics/...md`, `runs/...md`) are byte-deterministic
projections of JSON: identical inputs produce a byte-identical
Markdown file across runs (constitution §10; data-lake contract
§9). Editing them is a no-op: the next run wipes the edit. If a user
wants to add commentary, they should add a sibling `.md` file (not
one of the generated names) and Claude can read it, but Claude
should make clear that such files are not authoritative.

### 3. Writing JSON directly to processed/

Claude / MCP must never bypass the governed loop by writing under
`processed/meetings/<meeting_id>/`. The only way for a new artifact
to appear there is the existing `Produce → Evaluate → Decide →
Promote` loop and `writer.write_promoted_artifact`. A future MCP
integration that wants to "save Claude's notes" should:

1. Treat the note as a new RAW input under
   `raw/meetings/<meeting_id>/`, AND
2. Run it through the governed loop, OR
3. Persist it as a separate non-canonical artifact in a clearly
   non-`processed/` location.

### 4. Live model calls inside core

The constitution forbids live model calls in `spectrum-systems-core`
itself. An MCP server that runs Claude is allowed to read this repo
on the user's behalf, but it must never call into core's loop with a
live-model side effect injected mid-flight. Extractors are
deterministic by contract.

---

## Future MCP write access

If a future MCP integration enables Claude to write into the lake,
the writes must:

- create a **new governed artifact** (a new envelope with
  `status: draft`),
- run it through the standard loop (`evals → control → promote`),
- never silently mutate an existing artifact (`payload` is
  append-only by envelope; the constitution's "state changes are new
  envelopes" rule).

A markdown-style "edit in place" surface is incompatible with the
constitution and must not be added.

---

## Summary for Claude operators

- Read Markdown for context. Cite JSON when you make claims.
- Trust promoted artifacts. Diagnose with debug / harness memory.
- Don't edit generated Markdown.
- Don't write into `processed/` outside the governed loop.
- When in doubt, the binding documents are
  `docs/architecture/system_constitution.md` and
  `docs/contracts/data_lake_contract.md`.
