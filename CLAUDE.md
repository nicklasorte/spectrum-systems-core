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

Every Claude Code session that writes or modifies code MUST follow this loop
before opening a PR. No exceptions.

### Required loop

1. **BUILD** — implement the change
2. **SELF-REVIEW** — re-read every file written; attack:
   - Any path where a bad artifact could pass a gate silently
   - Any gate bypassable by missing input
   - Any failure a new engineer could not explain from the artifact alone
   - Any field using `artifact_kind` instead of `artifact_type`
   - Any step that only fails post-CI instead of pre-PR
3. **FIX** — fix every finding from the self-review
4. **VERIFY** — run automated verification scripts that simulate the actual
   failure case and prove the fix works. These must be inline Python/bash
   scripts in the session, not just `pytest`. At minimum:
   - Run the full test suite (`pytest`)
   - Simulate the specific failure the fix targets and assert it no longer occurs
   - Assert no regression on related paths
5. **RE-REVIEW** — re-read the fixed code; attack again
6. **FIX AGAIN** — fix any new findings
7. **OPEN PR** — PR description MUST include:
   - Output of all verification scripts (copy-paste, not paraphrase)
   - What each self-review pass found
   - What was fixed in response
   - Confirmation the simulated failure case now passes

### What "verify" means

Verification is not just `pytest`. It means writing a small script that
reproduces the exact failure that motivated the fix, running it, and
asserting it passes. Example: if the bug was a trailing space causing
a lookup failure, the verification script must pass `"value "` (with space)
to the fixed function and assert it succeeds.

### Enforcement

If a PR description is missing verification output, it must not be merged.
The operator should request the missing verification before approving.

### Auto-debug rule (non-negotiable)

When a workflow or script produces an unexpected result (wrong output,
silent success with bad data, missing marker files), Claude Code must:

1. Read the actual artifacts from the data-lake directly
2. Run a simulation that reproduces the exact failure state
3. Identify the root cause from the simulation output
4. Fix the root cause
5. Prove the fix with the same simulation

The operator must never need to manually click through Actions logs
or browse data-lake files to diagnose a failure. The Claude Code
session is the debugger.

### PR failure protocol (non-negotiable)

When a PR check fails, Claude Code MUST follow
`docs/governance/PR_FAILURE_PROTOCOL.md` before touching any code:

1. Run `_pr_triage.py` or read the actual failing log
2. Classify the failure using the taxonomy
3. If INFRASTRUCTURE: document, do not change code
4. If logic failure: root-cause first, minimum safe repair,
   governance hardening, no-weakening assertion
5. PR description must include all sections A–G from the protocol

Skipping the protocol and jumping directly to a code fix is a
CLAUDE.md violation. The goal is never "make CI green" — the goal
is to strengthen the governed system.

### Integration test requirement (non-negotiable)

Every Claude Code session that writes or modifies a script that reads a
pipeline artifact MUST also write or update an integration test that:

1. Uses `tests/integration/fixtures.py` factory functions to produce
   artifacts — NEVER hand-rolled dicts. The factory must call the
   actual writer (`ExtractionMerger.merge`, runner, etc.), not
   construct a dict manually.
2. Writes artifacts to a real temp directory (not mocked).
3. Calls the script via `subprocess.run` against the temp directory.
4. Asserts the correct output on disk (not just the return code).

This rule exists because unit tests with synthetic fixtures do not catch
field name mismatches between the writer and the reader — that is the
exact bug class that produced PRs #77 / #78 / #79. Integration tests
backed by `tests/integration/fixtures.py` catch the drift at the
fixture factory level, before the script logic runs.

Co-requirement: every script that reads a pipeline artifact MUST call
`scripts/_artifact_validator.validate_artifact` on the loaded artifact
before reading any field off it. This adds a second line of defence —
the script refuses to run on an artifact whose `artifact_type` or
schema shape has drifted, instead of failing mysteriously inside the
script's logic.

The canonical integration-contract file is
`tests/integration/test_script_artifact_contracts.py`. New per-script
contract tests should either land there as additional functions or in
a sibling `tests/integration/test_<script_stem>_contract.py` file.

**How to check compliance before opening a PR:**

```bash
python - <<'PY'
import pathlib, re, subprocess, sys

scripts_dir = pathlib.Path("scripts")
test_dirs = [
    pathlib.Path("tests/integration"),
    pathlib.Path("tests/scripts"),
]

ARTIFACT_TYPE_PATTERN = re.compile(
    r"validate_artifact|meeting_extraction|"
    r"correction_candidate|ground_truth_pair|human_review|"
    r"decision_few_shot_examples"
)
# A script "reads an artifact" only when it both references a known
# pipeline artifact type AND actually parses JSON off disk. Pure
# seeders/migrators that emit ``artifact_type`` strings without
# reading existing files are excluded.
READS_JSON_PATTERN = re.compile(r"json\.loads?\b|json\.load\(|read_text\(")

# Scope to scripts touched in the current PR. Pre-existing scripts that
# never had integration tests are tech debt; this rule applies to
# scripts the current session is writing or modifying.
try:
    # ``origin/main`` (no ``...HEAD``) compares the working tree against
    # main so the check catches uncommitted changes pre-PR.
    diff = subprocess.check_output(
        ["git", "diff", "--name-only", "origin/main", "--", "scripts/"],
        text=True,
    )
    touched = {pathlib.Path(p).name for p in diff.splitlines() if p.endswith(".py")}
    # Untracked new scripts also count as touched.
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "scripts/"],
        text=True,
    )
    touched.update(
        pathlib.Path(p).name for p in untracked.splitlines() if p.endswith(".py")
    )
except subprocess.CalledProcessError:
    # Fall back to all scripts when origin/main is unavailable.
    touched = {p.name for p in scripts_dir.glob("*.py")}

coverage_text_parts = []
for d in test_dirs:
    if d.is_dir():
        for p in d.glob("test_*.py"):
            coverage_text_parts.append(p.read_text(encoding="utf-8"))
coverage_text = "\n".join(coverage_text_parts)

missing = []
for script in sorted(scripts_dir.glob("*.py")):
    if script.name.startswith("_") or script.name not in touched:
        continue
    content = script.read_text(encoding="utf-8")
    if not (
        ARTIFACT_TYPE_PATTERN.search(content)
        and READS_JSON_PATTERN.search(content)
    ):
        continue
    if script.stem in coverage_text:
        continue
    missing.append(script.name)

if missing:
    print(f"MISSING integration tests for: {missing}")
    print("Add contract tests under tests/integration/ before opening PR.")
    sys.exit(1)
print("OK: all artifact-reading scripts touched in this PR have integration tests")
PY
```

The check is scoped to scripts touched in the current PR (via `git diff
origin/main...HEAD`) so it does not block on pre-existing scripts that
never had integration-test coverage. Coverage is accepted under either
`tests/integration/` (preferred for new scripts; the contract tests
there use the `tests/integration/fixtures.py` factories per the rule
above) OR `tests/scripts/` (historical location). New contract tests
MUST go under `tests/integration/` to satisfy the fixture-factory
clause.

Run this check as part of the pre-PR verification loop. The script is
intentionally short and shells out from a HEREDOC so it works in any
checkout without installing extra tooling.

### Artifact manifest requirement (non-negotiable)

`docs/architecture/artifact_manifest.md` is the single authoritative
list of every artifact type the pipeline writes to disk. Every Claude
Code session that adds a new artifact type, or that changes an
existing artifact's path, schema, or git-tracked status, MUST:

1. Update `docs/architecture/artifact_manifest.md` so the entry
   reflects the new path / schema / tracked status.
2. Run `python scripts/_gitignore_audit.py` and confirm it passes.
   The audit reads the manifest and asserts every "Git-tracked: YES"
   path template is NOT shadowed by any `.gitignore` rule.
3. If the artifact is read by a `scripts/*.py` consumer, add or
   update the factory function in `tests/integration/fixtures.py`
   so the integration-test layer can produce the artifact via the
   real writer (per the integration-test rule above).

Compliance check (run as part of pre-PR verification):

```bash
python scripts/_gitignore_audit.py
```

The audit must exit 0. If it fails, either un-ignore the path in the
appropriate `.gitignore` (mirror the existing `!**/processed/**/source_record.json`
pattern), move the artifact to a different on-disk path, or — if the
artifact is genuinely runtime-only — flip its manifest entry to
`Git-tracked: NO` and remove any workflow `git add` that targets it.

### Commit message hygiene — never spell out CI skip tokens

GitHub Actions silently skips ALL workflows on the head commit of a push
or PR when the head commit's title OR body contains any of these literal
substrings (the match is naive — backticks, code fences, and quotes do
NOT escape it):

- the bracketed `skip ci` token
- the bracketed `ci skip` token
- the bracketed `no ci` token
- the bracketed `skip actions` token
- the bracketed `actions skip` token

When the token lands in a head-commit message, `pytest` and `smoke-test`
never fire; the PR shows `pending` with zero check_runs and zero
statuses — easy to misdiagnose as "the approval gate" or "Actions is
slow" because GitHub does not emit an explanatory message.

This is a real foot-gun because several workflows in this repo
(`validate-and-baseline.yml`, `debug-single-transcript.yml`) intentionally
EMIT skip-ci commits to break loops, and describing that behaviour in
prose makes it tempting to paste the literal token.

Rule: when documenting these workflows in commit messages or PR
TITLES, refer to the token without the literal brackets. Acceptable
forms: "the skip-ci marker", "GitHub's skip-ci token", "skip-ci
(bracketed)", or `[ skip ci ]` with internal spaces. The same rule
applies to PR titles because the merge commit on `main` inherits the
title. Inside fenced code blocks in a PR BODY the token is safe —
GitHub only inspects commit messages, not PR bodies — but never inside
the title or any commit message in the chain.

If you discover a PR whose checks are mysteriously empty, run:

```bash
git log -1 --format='%B' | grep -nE '\[skip ci\]|\[ci skip\]|\[no ci\]|\[skip actions\]|\[actions skip\]'
```

A non-empty grep means the head commit message is the cause. Fix by
pushing a follow-up commit whose message omits the token (the new HEAD
re-triggers via the `synchronize` event) or by amending the commit
message and force-pushing the branch.

## Control is fail-closed

`control/decision.py` is the only place decisions come from. Rules:

- No eval results → `block` (`missing_required_evals`).
- Any eval result with `status == "fail"` → `block` (`failed:<eval_type>`).
- All required evals pass → `allow`.
- `warn` and `freeze` exist in `ALLOWED_DECISIONS` but are **reserved** and not used. Do not emit them.
- Model output never decides; only the control function does.

`promotion/promoter.py::promote_if_allowed` is the single path to `promoted` — promotion anywhere else is a constitution violation.

## Constitutional governance

`docs/architecture/system_constitution.md` is **binding** for this
repo. It is imported directly below. Re-read it before any
architectural change.

@docs/architecture/system_constitution.md

Key rules also kept inline for fast reference:

- The system has exactly one loop: **Produce → Evaluate → Decide → Promote**. Every module must serve it or be deferred.
- Top-level module names are fixed: `artifacts`, `context`, `workflows`, `evals`, `control`, `promotion`, `data_lake`. Adding a new top-level module requires amending the constitution.
- `failure_learning` and `ai_adapter` are reserved names but deliberately not implemented. So are live model calls, autonomous agents, dashboards, vector indexes, embeddings, semantic search, certification gates, and remote persistence. Do not add them.
- Reject AEX/PQX/EVL/TPA/CDE/SEL terminology from the predecessor `spectrum-systems` repo. Plain module names only.
- Prefer one artifact envelope over many families; one control model over many authority systems.

## Commands

```bash
pip install -e ".[dev]"          # install package + dev extras (pytest)
python -m pytest                 # run full suite
python -m pytest tests/test_artifact_model.py            # one file
python -m pytest tests/test_artifact_model.py::test_name # one test
python -m pytest -k grounding    # filter by name
```

CI runs only `python -m pytest` on Python 3.11 (`.github/workflows/pytest.yml`). There are no linters, formatters, type-checkers, or coverage gates configured — do not add them without a concrete need (`docs/development/ci.md`).

## Core loop architecture

The loop lives in `src/spectrum_systems_core/workflows/_loop.py::run_governed_loop`. Each workflow file (`meeting_minutes.py`, `decision_brief.py`, `agency_question_summary.py`, `meeting_action_log.py`) only supplies an `artifact_type` string and a deterministic `extract(input_text) -> dict` function. The loop does the rest:

1. `context.build_context_bundle` → context artifact
2. `artifacts.new_artifact` with the extracted payload (status `draft`)
3. `evals.run_required_evals` → list of `eval_result` artifacts
4. `control.decide_control` → `control_decision` artifact (`allow` / `block`)
5. `promotion.promote_if_allowed` → status becomes `promoted` only on `allow`, else `rejected`

### Adding a new artifact type

Two edits, no new modules:

1. Add `run_<type>_workflow` in `workflows/<type>.py` that calls `run_governed_loop` with a deterministic `extract` function. Re-export it from `workflows/__init__.py`.
2. Add the required-field tuple and entry in `evals/runner.py::REQUIRED_FIELDS_BY_TYPE`. The `non_empty_payload` eval already runs for every type.

If the type also needs to flow through the data lake pipeline, extend `_CONTENT_SIGNAL_KEYS_BY_TYPE` in `data_lake/pipeline.py` and `data_lake/extract.py`'s grounded payload builder.

## Artifact envelope (one schema for everything)

`artifacts/model.py::Artifact` has these fields and they are shared by every artifact type (target artifacts, `context_bundle`, `eval_result`, `control_decision`, `manifest`, etc.): `artifact_id`, `artifact_type`, `schema_version`, `status`, `created_at`, `trace_id`, `input_refs`, `content_hash`, `payload`. Statuses are restricted to `{draft, evaluated, promoted, rejected}` (`artifacts/validation.py`). State changes are new envelopes or status updates — never edit `payload` in place.

## Data lake boundary (`data_lake/`)

`docs/contracts/data_lake_contract.md` is binding for everything
under `data_lake/`. It is imported directly below.

@docs/contracts/data_lake_contract.md

### Determinism is the trust property

All outputs under `processed/` and `indexes/` must be byte-identical across runs given the same inputs. Use `data_lake/serialize.py::canonical_json` (sorted keys, stable separators, single trailing newline). The pipeline (`data_lake/pipeline.py`) achieves this by replacing UUIDs and wall-clock `created_at` with `_stable_artifact_id` (hash of kind+trace_id+payload) and `_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"` for every artifact it produces. If you add an artifact to the pipeline, run it through `_stabilize` or you will break the determinism invariant.

The `artifact_index.jsonl` is built only from promoted processed artifacts and is sorted by `(meeting_id, artifact_type, artifact_id)` before write.

### Pipeline-only evals

Beyond the type-required-field evals from `evals/runner.py`, the pipeline adds three more in `data_lake/pipeline.py`: `source_grounding`, `transcript_evidence` (blocks transcript-source runs that produced no grounded spans), and `content_signal` (blocks `notes`/`summary` runs whose content lists are all empty). All four pass through the same `decide_control`.

## Testing philosophy

From the constitution: tests defend trust properties, not ceremony. Add a test only if it falls into one of these categories:

- **Unit** — pure logic (artifact construction, hashing, decision rules).
- **Contract** — payloads conform to declared `schema_version` for their `artifact_type`.
- **Golden workflow** — known input → known artifact → known decision, end-to-end.
- **Fail-closed** — missing required evals block; failed required evals block; only `allow` leads to promotion.

Golden transcripts live in `tests/fixtures/golden_meetings/`.

## Taxonomy

All domain taxonomy lists (regulatory verbs, decision outcome types, claim types)
are defined in `src/spectrum_systems_core/config/taxonomy.py`. The extraction
prompt builder and the binding validator both import from this module so the
two cannot drift. Never define these lists inline in prompt templates or
validators. Tests assert `id()` equality on the imported objects.

## Phase planning protocol

Before starting a new phase planning cycle, the operator should run:

```
python -m spectrum_systems_core.cli next-phase-handoff
```

The output `prompt_opening` should be pasted into the new Claude
conversation as the seed for STEP 1 inventory. If the briefing's
`valid_until` is in the past, re-run `verify-pipeline-state` first, then
re-run `next-phase-handoff`.

## Files worth reading before non-trivial changes

- `docs/architecture/system_constitution.md` — binding; precedence over everything else.
- `docs/contracts/data_lake_contract.md` — binding for `data_lake/`.
- `src/spectrum_systems_core/workflows/_loop.py` — the loop in ~70 lines.
- `src/spectrum_systems_core/data_lake/pipeline.py` — the only place data-lake I/O meets the core loop.
- `docs/decisions/` — judgment records capturing why architectural decisions were made. Read all files here before any change that touches module structure, artifact types, or the core loop.
- `docs/decisions/2026-05-13-skl-sequencing.judgment_record.json` — specifically: SKL trace extraction pre-conditions. Do not begin SKL trace extraction work without verifying these are met.

## Operator env vars and phase wiring notes

Runtime configuration, feature flags, and phase wiring details are in
`docs/development/operator_reference.md`.
