# GitHub Actions Workflow Conventions

Document ID: SSC-CONV-001
Status: Binding for every workflow under `.github/workflows/` that an
operator dispatches from a phone (i.e. the GitHub mobile Actions UI).

This document is the first thing to read before writing or modifying any
GitHub Actions workflow in this repository. It captures hard-won rules
about the phone-safe dispatch shape, the failure-diagnostics contract,
single-purpose workflow design, the data-lake push pattern, and
fail-closed detection on apparent success.

The system constitution (`docs/architecture/system_constitution.md`)
and the data-lake contract (`docs/contracts/data_lake_contract.md`)
take precedence on any conflict. The rules here are operational —
they bind every workflow file, not the artifact envelope or the loop.

The `debug-llm-extraction.yml` workflow is the explicit anti-pattern
reference. It is intentionally excluded from these conventions: it
exists to host the boolean toggles and observe-only diagnostic modes
that the phone-safe workflows fan out into. Do not "fix" it.

---

## 1. Phone-safe workflow rules

### 1.1 Zero boolean inputs

Every workflow that is dispatched from the GitHub mobile Actions UI
MUST declare zero `type: boolean` inputs.

The reason is a documented mobile UX issue. GitHub's mobile dispatch
form caches the previous dispatch's boolean input values and surfaces
them already toggled in the next dispatch — the "sticky toggle"
problem. A boolean input that defaulted to `false` but was flipped to
`true` for one diagnostic run will silently remain `true` on the next
dispatch from the same device. The operator does not see the
inherited state until after the run has already started.

Workflows that already fan out branches off a boolean (e.g. a debug
mode, an observe-only flag, a cascade variant) end up running the
non-default branch by accident. Phone-safe workflows therefore avoid
the input type entirely.

Reference for the sticky-toggle motivation: the comment block at
`.github/workflows/run-cascade-filter.yml:23-26` is the canonical
in-repo explanation ("Zero boolean inputs by design — the
sticky-toggle problem on GitHub mobile is exactly why this workflow
exists").

### 1.2 String inputs are safe

Use `type: string` (or `type: choice` with explicit options) for every
phone-safe input. String inputs are not subject to the sticky-toggle
problem because the mobile form renders them as a text field, not a
checkbox.

When a binary flag is genuinely required (e.g. `use_cascade_output`
in `run-comparison.yml`), declare it as `type: string` with
`default: 'false'` and read it inside the step with an explicit
string comparison: `if [ "$INPUT" = "true" ]; then ...`. This pattern
is identical to the one already in production at
`.github/workflows/compare-opus-haiku.yml:142` (the `INCLUDE_SONNET`
read uses `[ "$INCLUDE_SONNET" = "true" ]` regardless of the input
declaration).

### 1.3 Source-id default is the Dec 18 transcript slug

Workflows that take a `source_id` input MUST pre-populate the
default with the canonical Dec 18 transcript slug so a phone
operator can dispatch with a single tap:

```
default: '7-ghz-downlink-tig-meeting-kickoff---transcript-20251218'
```

Reference: `.github/workflows/run-haiku-extraction.yml:29` is the
canonical example. The same default appears in
`.github/workflows/run-cascade-filter.yml:34` and
`.github/workflows/run-comparison.yml:26`.

### 1.4 YAML validation before the PR opens

Every new workflow MUST be YAML-validated before its PR opens. The
validator is one line:

```
python -c "import yaml; yaml.safe_load(open('.github/workflows/<name>.yml').read()); print('OK')"
```

The PR description must include the OK output for every workflow
file the PR touched.

---

## 2. Actionable diagnostics on failure (mandatory)

Every workflow step that can fail MUST surface actionable diagnostics
in the GitHub step summary. A generic "exit 1" or a bare "BLOCKED"
line is insufficient. The operator must be able to understand WHAT
failed and WHY from the step summary alone, without opening a debug
JSON file or re-running with extra flags.

### 2.1 Required content on failure

The failure block MUST emit, into `${GITHUB_STEP_SUMMARY}`, at minimum:

1. Per-eval reason codes — every `reason_codes=<value>` line that
   appeared on stdout or stderr. The CLI emits these on the BLOCKED
   line (exit 1) or the pre-run-halt line (exit 2). Grep them out
   explicitly so they survive into the summary.
2. The specific failing field or value where available (e.g.
   `failed:llm_extraction_strict_schema`,
   `None is not of type 'string' at ['scheduled_events', 0, 'event_id']`).
   The reason-code lines from item 1 carry this context; do not
   summarize it out.
3. The first 2000 chars of stderr (`head -c 2000 stderr.log`).
4. The last 40 lines of stdout (`tail -n 40 stdout.log`) — used
   either alongside stderr or in fail-closed-after-success blocks
   (see section 5).

### 2.2 Reference implementation

`.github/workflows/run-haiku-extraction.yml:100-118` is the canonical
failure block. Copy its shape verbatim:

```bash
{
  echo "## meeting-minutes-llm FAILED (exit ${rc})"
  echo ""
  echo "- source_id: \`${SOURCE_ID}\`"
  echo ""
  echo "### reason_codes"
  echo '```'
  grep -iE 'reason_codes?=' stdout.log stderr.log \
    || echo "(no reason_code line emitted)"
  echo '```'
  echo ""
  echo "### stderr (first 2000 chars)"
  echo '```'
  head -c 2000 stderr.log
  echo ""
  echo '```'
} >> "${GITHUB_STEP_SUMMARY}"
exit 1
```

The grep pattern is intentionally `reason_codes?=` (handles both
`reason_code=` and `reason_codes=` so future single/plural shifts in
the CLI do not break the surfacing). Do not narrow the pattern.

`.github/workflows/run-cascade-filter.yml:131-149` is the same block
adapted to the cascade workflow — same grep, same head/tail, same
exit semantics. Use whichever neighbor matches your workflow's shape.

---

## 3. Single-purpose workflows

Each phone-safe workflow does exactly one thing. There are no debug
toggles, no observe-only modes, no `print_raw_response` flags, no
`max_chunks` cap, no model dropdown on a production extraction
workflow. Those belong in dedicated debug workflows.

If a debug capability is needed, create a separate
`debug-<purpose>.yml` workflow rather than adding a boolean (or any
toggle) to a production workflow. The existence of
`.github/workflows/debug-llm-extraction.yml` is exactly this split:
it hosts every diagnostic mode (`max_chunks`, `single_chunk`,
`print_raw_response`, `minimal_repro`, `diff_vs_opus`,
`test_parser_only`, `cascade_filter`, `model` dropdown) so the
production-shape `.github/workflows/run-haiku-extraction.yml` can
stay one input wide.

Reference for the explicit "production-shape with no debug toggles"
intent: the comment block at
`.github/workflows/run-haiku-extraction.yml:3-21` ("Dedicated
one-input workflow for the full haiku meeting-minutes-llm run. No
debug toggles, no max_chunks, no single-chunk — this is the
production-shape extraction ...").

---

## 4. Data-lake push pattern

All data-lake writes go through the `./.github/actions/push-data-lake`
composite action. Hand-written `git clone … data-lake.git` or
`git push` from a workflow is a CLAUDE.md violation (see the
"Data-lake separation" section there).

### 4.1 Verify `add_paths` against the CLI's filename pattern

Before committing a workflow, the `add_paths` glob MUST be verified
against the CLI source that produces the artifact. The verification
is concrete: find the `print` call that emits the artifact's filename
and confirm the glob matches the literal pattern.

Examples from current code:

- `run-haiku-extraction.yml:151` writes
  `store/processed/meetings/${SOURCE_ID}/meeting_minutes__*.json`,
  which matches the CLI's `f"written={last_written}"` line at
  `src/spectrum_systems_core/cli.py:3423`. The `meeting_minutes__`
  prefix comes from the `<artifact_type>__<slug>.json` filename
  convention in `docs/contracts/data_lake_contract.md` section 6.
- `run-cascade-filter.yml:184-185` writes both
  `meeting_minutes__*.json` and `meeting_minutes_filtered__*.json`,
  matching `cli.py:3423` (raw `written=`) AND `cli.py:3580`
  (`filtered_written=`).

A glob that does not match a known CLI emit site is a bug — the
push will silently no-op (see section 4.2).

### 4.2 Include `.gitignore` in `add_paths`

`nicklasorte/data-lake` bulk-ignores `processed/`. A `git add` of a
file under that directory is a SILENT no-op unless the directory
chain AND the file are re-included first via a negation entry. The
workflow MUST therefore (a) add the negation rule to the cloned
data-lake's `.gitignore`, and (b) include `.gitignore` in the
`add_paths` block so the negation entry is part of the same commit
as the artifact.

Reference: `.github/workflows/run-haiku-extraction.yml:127-151` is
the canonical pattern. Lines 127-140 ensure the negation entry
(`!**/processed/**/` plus the artifact-specific negation), and the
`add_paths` block at 149-151 lists `.gitignore` first. Two writes
of the same artifact produce a byte-identical
`.gitignore` (the `add_rule` helper is idempotent).

The pattern mirrors the established
`!**/processed/**/source_record.json` precedent already in the
data-lake's `.gitignore`. Do not invent a new precedent.

---

## 5. Fail-closed detection after rc=0

A successful exit code (rc=0) is NOT sufficient evidence that the
workflow did what it was supposed to do. Several diagnostic modes
in `debug-llm-extraction.yml` produce rc=0 without writing the
promoted artifact at all; a phone-safe workflow that swallows that
case would report "PROMOTED OK" while the data-lake never received
the file (this was the exact bug `debug-llm-extraction.yml:11-21`
calls out).

### 5.1 Required check

After rc=0, verify the expected artifact was actually written. The
CLI prints the absolute artifact path on the success line as
`written=<path>` (for the raw extraction) or `filtered_written=<path>`
(for the cascade-filtered artifact). If the expected token is
absent from stdout after exit 0, the workflow MUST treat the run
as a failure: emit a step-summary block explaining "exited 0 but
wrote no artifact" and `exit 1`.

### 5.2 Reference implementations

- Raw extraction: `run-haiku-extraction.yml:75-87` greps
  `written=` from stdout and exits 1 if empty.
- Cascade extraction: `run-cascade-filter.yml:88-116` greps both
  `written=` and `filtered_written=`. A missing `filtered_written`
  after rc=0 means the cascade silently no-op'd — the whole point of
  the workflow — so the fail-closed branch fires.

The CLI emit sites that back these tokens are
`src/spectrum_systems_core/cli.py:3423` (`written=`) and
`src/spectrum_systems_core/cli.py:3580` (`filtered_written=`). When
adding a new fail-closed check, find the corresponding `print` call
and grep for the same literal token.

### 5.3 Cascade-style disambiguation

When stdout can carry multiple token shapes (e.g. `written=` AND
`filtered_written=` AND `log_written=`), the grep MUST disambiguate
on whitespace prefix to avoid matching the longer token's suffix.
`run-cascade-filter.yml:88-89` shows the pattern:

```bash
WRITTEN_PATH="$(grep -oE '(^|[[:space:]])written=[^[:space:]]+' stdout.log \
  | tail -n1 | sed -E 's/^[[:space:]]*written=//')"
```

Without the `(^|[[:space:]])` anchor, the grep would match the
`written=` substring inside `filtered_written=` and report the
filtered path as the raw path.

---

## 6. Verify-after-push (mandatory for data-lake writes)

After the `push-data-lake` composite step, re-fetch `origin/main`
and assert the exact artifact path the CLI reported is a real blob
in the pushed remote ref. Read the remote, not the runner's working
tree, so a silently-skipped `git add` is caught instead of reporting
a false success.

Reference: `.github/workflows/run-haiku-extraction.yml:153-189` is
the canonical verification step. The same shape appears at
`.github/workflows/run-cascade-filter.yml:187-223`.

---

## 7. Standing Inventory and Diagnosis Constraints

These rules apply to every Claude Code session that inventories the
repo or drafts a `fix(...)` PR — not only to workflow files. They sit
in this conventions doc because conventions are the first thing every
session reads before writing code; CLAUDE.md points here.

### 7.1 Built vs measured (mandatory in every inventory)

When a Claude Code session inventories existing capability in the
repo, every status it reports MUST be one of these five tokens:

- `present` — code exists, tests exist, **AND** measured end-to-end
  on a real artifact at least once. Evidence: a written artifact in
  the data-lake produced by this code path, or a green CI workflow
  run that exercised the path end-to-end.
- `present_never_measured` — code exists, tests exist, but the path
  has NEVER been run end-to-end on a real artifact.
- `partial` — some pieces exist, others missing.
- `missing` — nothing exists.
- `unknown` — could not determine from reading the code.

Status `present` is reserved for paths with end-to-end evidence.
Absent that evidence, the correct status is
`present_never_measured` — even when the code looks complete and the
unit tests pass.

Canonical reference: PR #226
(`fix(cascade): diagnose and fix items_dropped=0 / chunks_invalid=1`).
The cascade filter was treated as `present` for 23 PRs between PR #203
(its build) and PR #226. PR #226 was the first end-to-end measurement
on the Dec 18 transcript and immediately surfaced two bugs:
`_locate_chunk_for_item` had no routing logic for `turn_aggregate`
items (every such item piled into chunk 0), and the per-chunk filter
call had no `MAX_ITEMS_PER_FILTER_CALL` cap (Sonnet's response
truncated mid-JSON for the 230-item chunk). Both were invisible to
the unit suite because no test had run the executor against a real
transcript's chunk shape. Same pattern at
`src/spectrum_systems_core/cascade/executor.py` (the pre-PR-226 lines
the PR description quotes).

PR #214 (`feat(grounding): opt-in per-type min source_quote length
threshold`) is the secondary reference: the Stage 2 audit found that
the 1.4.0 verbatim-grounding schema, prompt instructions, per-item
fields, and Phase 6 cascade filter had ALL already shipped — work
the original roadmap had prescribed as duplicate. The only genuine
gap was the per-type minimum-length threshold. Without the built-vs-
measured distinction the audit would have re-derived the conclusion
that everything still needed building.

### 7.2 Stochastic vs structural diagnosis (mandatory before any `fix(...)` PR)

Before opening a PR whose title starts with `fix(...)`, a Claude Code
session MUST search prior commits AND prior closed PRs for the same
failure mode. The minimum search is two commands plus a GitHub PR
search:

```bash
git log --all --grep='<failing_field>' --oneline
git log --all --grep='<reason_code>' --oneline
```

…plus a GitHub PR search (via `mcp__github__search_pull_requests`)
on the same field name and reason code.

If the same failure mode has appeared in **2 or more prior PRs**, the
correct fix is NOT "fix this instance." The correct fix is "audit the
generating schema/constraint for the class of failure," because the
underlying cause is brittleness, not a bug.

Canonical reference: PR #228
(`fix(schema): add 'clarification' to position_type enum`). The PR
description itself names the same fix pattern as PR #182
(`attendees.agency` null) and PR #205 (`event_id` null) — three
instances of "a faithful Haiku extraction surfaces a real domain
value outside an over-narrow producer-facing constraint." Adding
`clarification` to one enum is the third one-at-a-time patch; the
durable fix is the class-wide schema enum audit in
`docs/audits/schema_enum_audit_2026_05.md`.

The trigger to switch from "fix this instance" to "audit the class"
is recurrence count ≥ 2. A first instance is a bug; a second is a
class. Treating a class as a third bug spends operator time
re-deriving the same conclusion.

### 7.3 Append-only mutation check (mandatory before any `fix(...)` PR that touches artifact-writing code)

Before opening a PR that modifies any code path that writes to the
data-lake, a Claude Code session MUST confirm the fix does not
mutate an existing artifact. The data-lake contract
(`docs/contracts/data_lake_contract.md` §8 "Boundary Rules") is
explicit: "Core never deletes anything. The data lake is append-only
from core's perspective."

Allowed patterns when a previously-written artifact needs to be
corrected or superseded:

- **Filter at boundary** — drop malformed entries at the aggregation
  seam before they extend the aggregate payload. Canonical reference:
  PR #212 (`fix(aggregation): empty batch overwrites required
  fields`), the `_is_well_formed_grounding_item` filter at
  `src/spectrum_systems_core/workflows/meeting_minutes_llm.py:1322`
  (the pre-PR-212 line the PR description quotes).
- **New artifact + discriminator** — write a new artifact carrying
  a discriminator field that downstream selection can filter on,
  leaving the old artifact in place. Canonical reference: PR #220
  (`fix(comparison): strategy-aware haiku artifact selection`), the
  `chunking_strategy_version` discriminator on `meeting_minutes`
  artifacts read by `scripts/compare_opus_haiku.py::find_candidate_artifact`.
- **Separate artifact path** — write the corrected artifact under a
  distinct filename so the original stays addressable. Canonical
  reference: PR #226 (`fix(cascade): diagnose and fix
  items_dropped=0 / chunks_invalid=1`), the Phase 6 cascade output
  at `meeting_minutes_filtered__*.json` alongside the raw
  `meeting_minutes__*.json` (data-lake contract §6 filename rule).

Disallowed pattern: in-place rewrite of an artifact that has been
promoted to the data-lake. Status changes happen by writing new
envelopes, never by editing payload in place (system constitution
§6: "State changes happen by writing new envelopes or updating
status fields, not by editing payload in place").

## 8. Checklist before opening a PR

For every workflow file added or modified in the PR:

1. Zero `type: boolean` inputs (unless the file is the documented
   debug anti-pattern at `.github/workflows/debug-llm-extraction.yml`).
2. `source_id` defaults to the Dec 18 transcript slug where
   applicable.
3. Every failure branch emits reason_codes + stderr head-2000 to
   `${GITHUB_STEP_SUMMARY}`.
4. Every rc=0 path checks the expected `written=` /
   `filtered_written=` token and exits 1 if absent.
5. Data-lake writes go through `./.github/actions/push-data-lake`
   and include `.gitignore` in `add_paths`.
6. `add_paths` glob has been verified against a concrete CLI
   `cli.py:<line>` emit site.
7. The verify-after-push step re-reads `origin/main` and fails on a
   missing blob.
8. YAML validates via
   `python -c "import yaml; yaml.safe_load(open('.github/workflows/<name>.yml').read())"`.
9. The PR description copy-pastes the YAML validation output.
