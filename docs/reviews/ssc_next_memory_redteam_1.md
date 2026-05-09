# SSC-NEXT-MEMORY — Red Team Review #1

Scope: SSC-025 (Obsidian vault layout), SSC-026 (frontmatter
hardening), SSC-027 (backlink generation), SSC-028 (meeting index
upgrade).

Question we are answering: does the new Markdown layout blur the
source-of-truth boundary, or stay safely inside the "view of JSON"
contract?

---

## Findings

### M1 (must_fix) — `canonical_json_path` could be empty in frontmatter

The frontmatter field `canonical_json_path` is built from the JSON
path passed by the CLI. If a future caller renders an artifact
markdown without supplying `canonical_json_path` (e.g. unit test or
reuse from a different orchestrator), the frontmatter will contain
`canonical_json_path:` with an empty value. A reader could then
mis-conclude that no JSON exists. Fix: keep the field present (so the
contract is uniform) but change the default to a clearly machine-
readable token like `(unwritten)` and add a regression test.

### M2 (must_fix) — backlinks block could mis-suggest authority

The artifact Markdown ends with a "Links" section that includes
`[[Meeting/<id>]]`, `[[Agency/<x>]]` and `[[Topic/<y>]]` wikilinks.
Without a one-line "JSON is the source of truth" reminder near those
wikilinks, a casual reader could think the wikilinks are an
authoritative graph maintained by core. The current code does insert
a `_BACKLINK_NOTE` BEFORE the links block, which is correct, but the
note language ("edits to it are not read by core") doesn't mention
"JSON is canonical" explicitly. Tighten the wording so the boundary
is unambiguous.

### M3 (must_fix) — index does not show that it is non-canonical

`status: view` and `canonical: false` are present in the index
frontmatter, but the body of `index.md` does not visibly say "this is
a regenerated view." A new engineer reading the file in Obsidian may
miss the frontmatter and treat the index page as authoritative. Fix:
add a short note at the top of the body that says "JSON is
canonical."

### S1 (should_fix) — `agencies/<slug>.md` has no link back to the canonical agency value

When the metadata `agency` value contains spaces or punctuation, the
filename uses the slug. The body says "Agency: FCC" but does not
explicitly link the slugged path back to the original string. Add a
frontmatter `agency: <original-string>` field (already done) and a
single body line "Original agency string: `FCC`" so a reader can
verify the slug→string mapping without reading metadata.json.

### S2 (should_fix) — backlinks block uses `(../index.md)` which assumes a fixed depth

The artifact Markdown lives under `markdown/artifacts/<type>.md` and
the index lives at `markdown/index.md`, so `../index.md` is correct
today. If the layout changes, this hardcoded relative link breaks.
Acceptable for now (the layout is binding by SSC-025); document it in
the docstring and add a single-line constant so the depth assumption
is explicit.

### D1 (defer_with_reason) — per-meeting agency note vs. cross-meeting agency index

A real Obsidian vault would benefit from a single
`indexes/agencies/FCC.md` listing every meeting that referenced FCC.
That file would span multiple meetings and would need to be rebuilt
whenever any meeting changes. Defer: this phase is per-meeting and
adding cross-meeting projections expands the data lake contract
beyond the per-meeting writer. Reason recorded.

### D2 (defer_with_reason) — Markdown views for `manifest__*.json` and `debug__*.json`

A reader looking at a blocked workflow currently has the index +
debug JSON. A Markdown rendering of the debug report would be more
readable, but the run-note Markdown (`runs/<run_id>.md`) added in
SSC-031 already covers the same ground in a friendlier shape. Defer.

---

## Boundary check

- Markdown is regenerated from JSON: yes (cli.py runs writer first,
  then markdown).
- Core never reads Markdown back: yes (no module imports markdown
  files; only JSON is read).
- Markdown is excluded from the artifact index: yes
  (`collect_index_records` only walks `*.json` at the meeting top
  level, never the `markdown/` subdir).
- Frontmatter fields claim authority: no (`canonical: false` for
  views, `status: view` for the index, `status: promoted` only on
  artifact md whose JSON is canonical).

---

## Usability check

- A new engineer can find canonical JSON from any artifact md:
  yes (frontmatter `canonical_json_path` + body link). Strengthen
  with M1.
- A human can understand a blocked workflow:
  yes (index lists reason codes + plain-English explanation; the
  per-run note also explains).
- Links resolve: yes (validated by
  `test_no_broken_relative_links_in_artifact_markdown`).
- Did the implementation add unnecessary architecture? No new
  top-level module; the new files live under `data_lake/` per the
  constitution. The agency / topic / runs subdirs are filename
  conventions, not modules.

---

## Classification

- must_fix: M1, M2, M3
- should_fix: S1, S2
- defer_with_reason: D1, D2
