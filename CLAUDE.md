# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What belongs in this file

CLAUDE.md contains only session governance: rules Claude Code must
follow, architectural invariants, PR requirements, and module
boundaries. Every line must answer: "would removing this cause Claude
to make a mistake on a code change?" If not, it belongs in
`docs/development/operator_reference.md`, not here.

Before appending anything to CLAUDE.md, apply this test. If it fails,
open `docs/development/operator_reference.md` instead.

## Claude Code Execution Standard (non-negotiable)

Every Claude Code session that writes or modifies code MUST follow this
loop before opening a PR. No exceptions.

### Required loop

1. **BUILD** — implement the change.
2. **SELF-REVIEW** — re-read every file written; attack:
   - Any path where a bad artifact could pass a gate silently.
   - Any gate bypassable by missing input.
   - Any failure a new engineer could not explain from the artifact alone.
   - Any field using `artifact_kind` instead of `artifact_type`.
   - Any step that only fails post-CI instead of pre-PR.
3. **FIX** — fix every finding.
4. **VERIFY** — run inline Python/bash scripts (not just `pytest`)
   that simulate the actual failure case and prove the fix works.
   At minimum: full test suite, targeted reproduction script,
   regression check on related paths.
5. **RE-REVIEW** — re-read the fixed code; attack again.
6. **FIX AGAIN** — fix any new findings.
7. **OPEN PR** — description MUST include verification output
   (copy-paste, not paraphrase), what each self-review found, what
   was fixed, and confirmation the simulated failure now passes.

### What "verify" means

Verification is not just `pytest`. It means writing a small script that
reproduces the exact failure that motivated the fix, running it, and
asserting it passes. Example: if the bug was a trailing space causing
a lookup failure, the verification script must pass `"value "` (with
space) to the fixed function and assert it succeeds.

If a PR description is missing verification output, it must not be
merged.

### Auto-debug rule (non-negotiable)

When a workflow or script produces an unexpected result (wrong output,
silent success with bad data, missing marker files), Claude Code must:

1. Read the actual artifacts from the data-lake directly.
2. Run a simulation that reproduces the exact failure state.
3. Identify the root cause from the simulation output.
4. Fix the root cause.
5. Prove the fix with the same simulation.

The operator must never need to manually click through Actions logs or
browse data-lake files to diagnose a failure. The Claude Code session
is the debugger.

### Imported sub-protocols

These imports are operative — read them when their trigger fires.

@.claude/pr_failure_protocol.md
@.claude/integration_test_requirement.md
@.claude/artifact_manifest_requirement.md
@.claude/rollback_contracts.md
@.claude/commit_message_hygiene.md

## Behavioral Principles (Karpathy)

Four principles addressing the most common mistakes in this codebase.
The Execution Standard governs the loop; these govern the thinking.

### 1. Think Before Coding

State assumptions before writing code.

- Read the schema before editing a field name — don't assume
  `text`/`assignee` when it says `action`/`owner` (PR #247).
- Adding an optional field to one array type → audit every sibling
  (PR #248: `source_turns` added to `topics` only, 8 types missed).
- Adding a cascade filter → read the existing one; no parallel
  implementations (PR #246: Phase 2.C left coexisting with 4.C).
- "Add optional X to type Y" → ask whether X applies to other types
  and state the answer before coding.

### 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No schema fields "for future use".
- No CLI flags not wired and tested end-to-end.
- No error handling for conditions the fail-closed pipeline prevents.
- Existing gate (e.g. `promotion/grounding_gate.py`) → import, don't
  rebuild.

### 3. Surgical Changes

Touch only what the task requires.

- Change only the section the task names; don't restructure adjacent
  code.
- Deprecating a workflow → rename `*-deprecated.yml`, don't delete.
- Unrelated finding → mention in PR description, don't fix silently.
- Match existing field ordering, indentation, and comment style.

Remove imports your changes made unused; don't remove pre-existing
dead code. Every changed line must trace to the PR's stated goal.

### 4. Goal-Driven Execution

Express every task as a verifiable outcome, not an imperative:

- "Add `source_quote` to action_items" → write a test that fails when
  `source_quote` is missing, then pass it.
- "Fix the cascade filter" → write a test reproducing the modify-span
  bug, then fix it.
- "Raise `max_tokens`" → write a test proving prior truncation is
  gone, then raise.

State a brief plan — each step paired with its verification. Weak
criteria ("make it work") produce field-name class bugs.

### Spectrum Systems pre-PR checks

Run before every PR:

1. **Schema field names.** `action_items` use `action` + `owner`, not
   `text` + `assignee`. Cross-check prompt examples against the
   `*.schema.json`.

2. **Array type completeness.** When adding an optional field to one
   array type in `meeting_minutes.schema.json`, audit siblings:
   ```bash
   python -c "
   import json
   s = json.load(open('src/spectrum_systems_core/schemas/meeting_minutes.schema.json'))
   for typ, defn in s['properties'].items():
       props = defn.get('items', {}).get('properties', {})
       if props and 'source_turns' not in props:
           print(f'MISSING source_turns: {typ}')
   "
   ```

3. **`artifact_type` never `artifact_kind`** (reinforces the
   Execution Standard self-review item):
   ```bash
   grep -rn "artifact_kind" src/ scripts/ tests/
   ```
   Compare against the pre-PR baseline; the count must not grow. New
   code uses `artifact_type`; legacy `obsidian_bridge` usages are
   tracked separately.

4. **Model string discipline.** Model strings live in constants, not
   inline literals. New code must not add inline literals:
   ```bash
   grep -rn 'claude-haiku\|claude-sonnet\|claude-opus' src/ scripts/ \
     | grep -v 'model_id\s*=\s*"\|MODEL\s*=\s*"\|# \|\.md\|\.json'
   ```
   Compare against the pre-PR baseline; the count must not grow.

5. **Single active workflow per pipeline step.** Before adding a new
   workflow, `ls .github/workflows/run-*.yml`. If a prior version
   exists, deprecate it.

## Governing documents

- `docs/governance/PR_FAILURE_PROTOCOL.md` — failure classification
  and remediation taxonomy referenced by the PR-failure protocol.
- `docs/governance/ENGINEERING_PRINCIPLES.md` — engineering
  principles binding on every session.

## Data-lake separation (non-negotiable)

All pipeline artifacts, transcripts, minutes, and other data files
live in the **`nicklasorte/data-lake`** repository.
`spectrum-systems-core` is code only.

Workflows access the data-lake via the
`./.github/actions/clone-data-lake` composite action (uses the
`DATA_LAKE_TOKEN` PAT). Pushes back go through
`./.github/actions/push-data-lake`. Both actions live under
`.github/actions/`.

Rules:

- **NEVER commit data into spectrum-systems-core.** The repo-root
  `.gitignore` carries `data-lake/` so a stray `git add data-lake`
  is a no-op. `_gitignore_audit.py` asserts that rule is present.
- **NEVER reference `DATA_LAKE_TOKEN` outside the secret context.**
  The PAT must only appear as `${{ secrets.DATA_LAKE_TOKEN }}` (in
  `with:` blocks for composite actions) or in `env:` blocks. Never
  echo it, never embed it in a commit message, never write it to
  disk.
- **NEVER push to spectrum-systems-core from a pipeline workflow.**
  Pipeline workflows push to `nicklasorte/data-lake`, never to
  spectrum-systems-core's `main`.
- When the data-lake is not on disk (e.g. forked PR, dev checkout
  without the token), tests and audits that depend on live
  data-lake files skip cleanly. The contract still binds; it is
  verified at a different time.

If a new workflow needs to touch artifacts, use the two composite
actions above. Do not hand-write `git clone …data-lake.git` or
`git push` of data into spectrum-systems-core.

## Control is fail-closed

`control/decision.py` is the only place decisions come from. Rules:

- No eval results → `block` (`missing_required_evals`).
- Any eval result with `status == "fail"` → `block`
  (`failed:<eval_type>`).
- All required evals pass → `allow`.
- `warn` and `freeze` exist in `ALLOWED_DECISIONS` but are
  **reserved** and not used. Do not emit them.
- Model output never decides; only the control function does.

`promotion/promoter.py::promote_if_allowed` is the single path to
`promoted` — promotion anywhere else is a constitution violation.

## Constitutional governance

`docs/architecture/system_constitution.md` is **binding** for this
repo. It is imported directly below. Re-read it before any
architectural change.

@docs/architecture/system_constitution.md

Key rules also kept inline for fast reference:

- The system has exactly one loop: **Produce → Evaluate → Decide →
  Promote**. Every module must serve it or be deferred.
- Top-level module names are fixed: `artifacts`, `context`,
  `workflows`, `evals`, `control`, `promotion`, `data_lake`. Adding
  a new top-level module requires amending the constitution.
- `failure_learning` and `ai_adapter` are reserved names but
  deliberately not implemented. So are live model calls,
  autonomous agents, dashboards, vector indexes, embeddings,
  semantic search, certification gates, and remote persistence.
  Do not add them.
- Reject AEX/PQX/EVL/TPA/CDE/SEL terminology from the predecessor
  `spectrum-systems` repo. Plain module names only.
- Prefer one artifact envelope over many families; one control
  model over many authority systems.

## Core loop architecture

The loop lives in
`src/spectrum_systems_core/workflows/_loop.py::run_governed_loop`.
Each workflow file (`meeting_minutes.py`, `decision_brief.py`,
`agency_question_summary.py`, `meeting_action_log.py`) only supplies
an `artifact_type` string and a deterministic
`extract(input_text) -> dict` function. The loop does the rest:

1. `context.build_context_bundle` → context artifact.
2. `artifacts.new_artifact` with the extracted payload (status
   `draft`).
3. `evals.run_required_evals` → list of `eval_result` artifacts.
4. `control.decide_control` → `control_decision` artifact (`allow` /
   `block`).
5. `promotion.promote_if_allowed` → status becomes `promoted` only
   on `allow`, else `rejected`.

### Adding a new artifact type

Two edits, no new modules:

1. Add `run_<type>_workflow` in `workflows/<type>.py` that calls
   `run_governed_loop` with a deterministic `extract` function.
   Re-export it from `workflows/__init__.py`.
2. Add the required-field tuple and entry in
   `evals/runner.py::REQUIRED_FIELDS_BY_TYPE`. The
   `non_empty_payload` eval already runs for every type.

If the type also needs to flow through the data lake pipeline,
extend `_CONTENT_SIGNAL_KEYS_BY_TYPE` in `data_lake/pipeline.py`
and `data_lake/extract.py`'s grounded payload builder.

## Artifact envelope (one schema for everything)

`artifacts/model.py::Artifact` has these fields and they are shared
by every artifact type (target artifacts, `context_bundle`,
`eval_result`, `control_decision`, `manifest`, etc.):
`artifact_id`, `artifact_type`, `schema_version`, `status`,
`created_at`, `trace_id`, `input_refs`, `content_hash`, `payload`.
Statuses are restricted to `{draft, evaluated, promoted, rejected}`
(`artifacts/validation.py`). State changes are new envelopes or
status updates — never edit `payload` in place.

The Phase Y comparator artifact is named
`extraction_alignment_comparison` (not `extraction_comparison`) to
avoid collision with the pre-existing Phase AB.3 instrument. Use
this name in all future phases.

## Data lake boundary (`data_lake/`)

`docs/contracts/data_lake_contract.md` is binding for everything
under `data_lake/`. It is imported directly below.

@docs/contracts/data_lake_contract.md

### Determinism is the trust property

All outputs under `processed/` and `indexes/` must be byte-identical
across runs given the same inputs. Use
`data_lake/serialize.py::canonical_json` (sorted keys, stable
separators, single trailing newline). The pipeline
(`data_lake/pipeline.py`) achieves this by replacing UUIDs and
wall-clock `created_at` with `_stable_artifact_id` (hash of
kind+trace_id+payload) and
`_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"` for every
artifact it produces. If you add an artifact to the pipeline, run
it through `_stabilize` or you will break the determinism invariant.

The `artifact_index.jsonl` is built only from promoted processed
artifacts and is sorted by `(meeting_id, artifact_type,
artifact_id)` before write.

### Pipeline-only evals

Beyond the type-required-field evals from `evals/runner.py`, the
pipeline adds three more in `data_lake/pipeline.py`:
`source_grounding`, `transcript_evidence` (blocks transcript-source
runs that produced no grounded spans), and `content_signal` (blocks
`notes`/`summary` runs whose content lists are all empty). All four
pass through the same `decide_control`.

## Testing philosophy

From the constitution: tests defend trust properties, not ceremony.
Add a test only if it falls into one of these categories:

- **Unit** — pure logic (artifact construction, hashing, decision
  rules).
- **Contract** — payloads conform to declared `schema_version` for
  their `artifact_type`.
- **Golden workflow** — known input → known artifact → known
  decision, end-to-end.
- **Fail-closed** — missing required evals block; failed required
  evals block; only `allow` leads to promotion.

Golden transcripts live in `tests/fixtures/golden_meetings/`.

## Taxonomy

All domain taxonomy lists (regulatory verbs, decision outcome types,
claim types) are defined in
`src/spectrum_systems_core/config/taxonomy.py`. The extraction
prompt builder and the binding validator both import from this
module so the two cannot drift. Never define these lists inline in
prompt templates or validators. Tests assert `id()` equality on the
imported objects.

## Debugging

When a bug resists an obvious fix, invoke `/debug` to apply the
scientific method protocol from
[DEBUGGING.md](./DEBUGGING.md). Do not attempt repeated speculative
fixes without it.

## Slash commands

The non-negotiable protocols above are enforced via slash commands.
Use these instead of re-reading the relevant section during a
session.

| Command | When to use |
|---|---|
| `/ship` | Before every PR — enforces the Claude Code Execution Standard |
| `/pr-failure` | When any PR check fails — enforces the PR failure protocol |
| `/integration-check` | Before `/ship` when scripts were touched — enforces the integration test requirement |
| `/debug` | When a bug resists an obvious fix, or when `/ship` verification fails |

Do not declare a fix complete if any command is failing. If tests
fail after an edit, treat it as a signal to invoke `/debug`.

Operator-only reference (commands, env vars, phase wiring, files
worth reading) lives in `docs/development/operator_reference.md` —
read on demand, not as a precondition.
