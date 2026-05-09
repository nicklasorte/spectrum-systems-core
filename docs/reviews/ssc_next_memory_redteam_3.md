# SSC-NEXT-MEMORY — Red Team Review #3

Scope: SSC-038 (Claude / MCP integration guide), SSC-039 (Dataview
examples).

Question we are answering: do these integration docs accidentally
invite a future Claude / Obsidian setup to drift past the
constitution's boundary?

---

## Findings

### M6 (must_fix) — `claude_mcp_obsidian.md` does not mention determinism

The integration guide says "Markdown is regenerated on every run" but
does not connect that statement to the constitution's
**determinism** rule (`docs/architecture/system_constitution.md` §10
and the data-lake contract §9). A reader who has not read the
constitution might think "regenerated" means "rewritten with new
content," when in fact identical inputs always produce a
byte-identical Markdown file. Fix: add a sentence so the guide says
"regenerated and byte-identical" and links the contract.

### M7 (must_fix) — Dataview examples reference frontmatter fields that the contract does not pin

The Dataview examples query on `artifact_type = "agency_note"` and
`artifact_type = "topic_note"`. Those values are written today by
`data_lake/markdown.py::render_agency_markdown` and
`render_topic_markdown`, but the contract (`§6.3`) only pins
`artifact_type`, `meeting_id`, `date`, `title`, `status`, `trace_id`
in frontmatter. If a future change renames `agency_note` /
`topic_note`, the Dataview snippets break silently.

Fix: extend `data_lake_contract.md` §6.3 to enumerate the full set of
`artifact_type` values used by the Markdown layer, and call out that
`agency_note` / `topic_note` / `run_note` / `meeting_index` are also
binding tokens for view files. Add a regression test that asserts
each value is present in some rendered file under a fixture meeting.

### S5 (should_fix) — guide does not warn about plugin lock-in

The Dataview doc says the plugin is optional, but it does not warn
that some queries (notably the `file.outlinks` example) require
Dataview-specific syntax. A reader looking for "all agencies my
meeting touched" with plain Markdown tooling will be surprised. Fix:
add a single line above each plugin-specific snippet noting that the
syntax is Dataview-only.

### D5 (defer_with_reason) — sample MCP server config

The integration guide does not include a sample MCP server config or
session log. That is intentional for now: this repo has no live MCP
integration. Adding a sample would either pretend something works
that does not, or specify a client setup that we have not tested.
Defer until a real MCP integration lands.

### D6 (defer_with_reason) — agency / topic global indexes

A vault-wide "all FCC artifacts ever" page would be more useful than
the per-meeting agency note. The per-meeting writer cannot build it
in one call without scanning the whole lake. Defer per RT#1's D1.

---

## Did we add too much before real usage?

The integration docs are guidance only. They cost nothing at
runtime and prevent a real misuse case (Claude treating Markdown as
canonical). They are kept short so a reader can absorb them in one
pass. Acceptable.

---

## Classification

- must_fix: M6, M7
- should_fix: S5
- defer_with_reason: D5, D6
