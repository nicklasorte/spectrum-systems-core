# PR Failure Protocol
# Version: 1.0.0
# Governs: all Claude Code sessions responding to a failing PR check
# Authority: CLAUDE.md ("Integration test requirement" + "Auto-debug rule")

## Purpose

When a PR check fails, the instinct is to make CI green as fast as possible.
That instinct is wrong. The goal is to strengthen the governed system so
this class of failure is understood, prevented earlier, detected earlier,
normalized into the control systems, replayable, and measurable.

A fix that makes CI green by weakening governance is worse than leaving
the PR red.

---

## Step 0: Auto-triage (MANDATORY, runs before any other step)

Before reading the error or touching any code, Claude Code runs:

```bash
python scripts/_pr_triage.py --pr-number <PR> --workflow <workflow_name>
```

This script:

1. Reads the failing workflow's step logs from .github/workflows/ history
2. Classifies the failure into one of the categories below
3. Emits a structured triage artifact to stdout
4. If classification is INFRASTRUCTURE: stops and reports, no code change made

If `_pr_triage.py` is not yet available, Claude Code reads the actual
failing step log by running:

```bash
gh run view <run_id> --log | grep -A 20 "error:\|Error:\|FAIL\|exit code"
```

Never proceed to Step 1 without reading the actual error text from the repo.

---

## Failure Classification Taxonomy

### Class I: INFRASTRUCTURE (fast-path — no code change)

Failures caused by the CI environment, not the code:

- `pyo3_runtime.PanicException` — missing C binding in runner env
- `_cffi_backend` missing — Python extension not compiled
- Network timeout during dependency install
- GitHub Actions runner OOM
- Pre-existing failures that reproduce on main with changes stashed

**Protocol:** Document the failure class. Confirm it reproduces on main
without the PR changes (`git stash && run tests`). If confirmed infrastructure:
add to `docs/governance/known_infrastructure_failures.md` with date and
runner context. No code change. Proceed with PR merge if all logic checks pass.

### Class II: SCHEMA DRIFT

Artifact field names, types, or required fields diverged between writer and reader.
Examples: `source_id` vs `source_artifact_id`, `provenance.phase` not in schema.

**Protocol:** Fix the schema or the writer (never the reader). Run
`scripts/_gitignore_audit.py` and `scripts/_artifact_validator.py` to confirm
no related drift. Add contract test using `tests/integration/fixtures.py`
factories (never hand-rolled dicts).

### Class III: GITIGNORE / PATH MISMATCH

Pipeline writes artifacts to a path that git ignores or that downstream
readers don't know about.

**Protocol:** Run `scripts/_gitignore_audit.py`. Fix the `.gitignore` negation
rule. Update `docs/architecture/artifact_manifest.md`. Never change the
reader's path to match a wrong writer path.

### Class IV: CONTRACT MISMATCH

Script expects artifact in format the writer doesn't produce.
Examples: missing `decisions` field, wrong `artifact_type` value.

**Protocol:** Read both the writer and the reader before touching either.
The writer is the source of truth for data shape. Fix the reader or add
a migration. Add integration test that uses the real writer output.

### Class V: SEQUENCING GAP

A workflow step requires input that a prior step was supposed to produce
but didn't (missing GT pairs, missing chunks.jsonl, missing source_record).

**Protocol:** Trace the full dependency chain. Find which step was supposed
to produce the input. Fix the producing step or add a generating script.
Update `docs/runbooks/first_run.md` to document the correct sequence.

### Class VI: GOVERNANCE WEAKENING ATTEMPT

A proposed fix disables a test, loosens an assertion, adds an allowlist,
converts a hard failure to a warning, or bypasses a promotion gate.

**Protocol:** BLOCK the PR. Do not merge. Open a new issue documenting
the governance violation. The fix must be rearchitected to preserve all
enforcement.

### Class VII: NONDETERMINISM / FLAKE

Test fails intermittently without a code change.

**Protocol:** Run the test 3 times. If it passes 2 of 3: document as
known flake, add `@pytest.mark.flaky` with a GitHub issue link. Never
delete or skip a flaky test without understanding why it flakes.

### Class VIII: MISSING EVAL COVERAGE

A behavior exists in production that has no test. The bug was only
discovered at runtime.

**Protocol:** Write the test first, confirm it fails, then fix. The test
must use real artifact shapes (fixture factories, not hand-rolled dicts).
The test must be added to the integration suite before the fix PR opens.

---

## Step 1: Root-cause identification (MANDATORY before any code change)

After triage, identify:

- **Exact technical cause** (the specific line/field/path that failed)
- **Triggering path** (what sequence of steps led to the failure)
- **Why CI failed** (what assertion, check, or command surfaced it)
- **Why upstream systems missed it** (which guard should have caught it earlier)
- **Failure class** (from taxonomy above)
- **Canonical owner subsystem** (from system_registry.md)

Do not open an editor until root cause is documented in the session log.

---

## Step 2: Minimum safe repair

Rules:

- Fix the root cause, not the symptom
- Prefer extending existing enforcement hooks over adding new ones
- Prefer centralized validators over scattered patches
- Never duplicate logic that already exists in `scripts/`, `tests/integration/`
- Every new file must be referenced from `artifact_manifest.md` if it produces artifacts
- Every new script that reads an artifact must have a contract test in `tests/integration/`
- The gitignore audit must pass after the fix

Anti-patterns (any of these = STOP and rearchitect):

- Disabling a test
- Loosening a schema assertion
- Adding `# noqa` or `# type: ignore` without structural justification
- Converting `sys.exit(1)` to `sys.exit(0)` in a guard script
- Moving validation into a comment
- Adding an exception for a specific source_id

---

## Step 3: Governance hardening (required for all Class II–VIII failures)

After the minimum safe repair, ask: which upstream check would have caught
this before the PR was opened? Add that check. Options:

- Add to `scripts/_gitignore_audit.py` manifest scan
- Add to `scripts/_env_validate.py` startup gate
- Add contract test in `tests/integration/test_script_artifact_contracts.py`
- Add to `tests/e2e/test_pipeline_smoke.py` smoke sequence
- Add step to `_few_shot_preflight.py` or `_artifact_validator.py`
- Add rule to `CLAUDE.md` pre-PR verification loop

The hardening is not optional. A fix without upstream hardening is incomplete.

---

## Step 4: No-weakening assertion (MANDATORY before PR opens)

Explicitly confirm each item:

```
[ ] No governance was bypassed
[ ] No tests were improperly weakened
[ ] No fail-closed protections were removed
[ ] No authority boundaries were weakened
[ ] No registry ownership boundaries were violated
[ ] No replay guarantees were weakened
[ ] No trace guarantees were weakened
[ ] No promotion discipline was weakened
[ ] artifact_manifest.md updated if new artifact type added
[ ] _gitignore_audit.py passes
[ ] _env_validate.py passes
[ ] Integration contract test added or updated
[ ] CLAUDE.md updated if new rule required
```

If any item cannot be checked: STOP. Fix the gap before opening the PR.

---

## Step 5: PR description requirements

Every PR that repairs a failing check must include:

**A. ROOT CAUSE** — exact technical cause, triggering path, failure class

**B. SYSTEM GAP** — which governance gap allowed this through, which invariant was weak

**C. REPAIR** — exact files changed, why this is minimum safe

**D. HARDENING** — what upstream detection was added, which script/test/rule

**E. FAILURE MODE ANALYSIS** — what related failures are now prevented, what risks remain

**F. NO-WEAKENING ASSERTION** — the checklist above, all items checked

**G. VERIFICATION OUTPUT** — actual command output proving the fix works:

- `python scripts/_gitignore_audit.py` → exit 0
- `python scripts/_env_validate.py --data-lake data-lake/ --skip-env` → exit 0
- `python -m pytest tests/integration/ tests/e2e/ -q` → all pass
- The specific failing test now passes
- Infrastructure failures: `git stash && pytest <test>` reproduces on main

---

## Known infrastructure failures

See `docs/governance/known_infrastructure_failures.md`.
These are pre-existing environment failures that reproduce on main.
They are documented, not fixed, unless the infrastructure is upgraded.

Current known failures (as of 2026-05-13):

- `tests/ingestion/test_pdf_extractor.py` — `pyo3_runtime.PanicException`
  from missing `_cffi_backend` in GitHub Actions runner (cryptography binding)
- `tests/ingestion/test_prepare_pdf_cli.py` — same root cause

These 11 failures do not block PR merges. They are tracked in
GitHub issue #[TBD].
