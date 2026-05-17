---
description: Required triage protocol when a PR check fails. Run before touching any code.
---

A PR check has failed. Do not touch any code yet. Follow this protocol in order.

## Step 1 — TRIAGE
Run `python _pr_triage.py` or read the actual failing log directly.
Do not infer the failure from the error message alone — read the log.

State:
- Which check failed
- The exact error message or assertion
- The file and line number where it failed

## Step 2 — CLASSIFY
Classify the failure using this taxonomy:

- **INFRASTRUCTURE** — environment issue, flaky network, missing secret, runner timeout.
  The code is not the cause.
- **LOGIC** — a test is asserting something the code got wrong. The code is the cause.
- **CONTRACT** — a field name, schema, or artifact type has drifted between writer and reader.
- **GOVERNANCE** — the PR violated a CLAUDE.md rule (missing verification output,
  wrong branch, wrong commit format).

State the classification explicitly. If uncertain between two classes, state both and explain.

## Step 3 — RESPOND BY CLASS

**If INFRASTRUCTURE:**
- Document what the infrastructure failure was
- Do not change any code
- Re-run the check to confirm it was transient
- If it persists, escalate to the operator

**If LOGIC:**
- Identify the root cause (not just the symptom)
- Design the minimum safe repair — the smallest change that fixes the root cause
  without weakening any gate
- Add a no-weakening assertion: a test that would fail if the gate were softened
- Fix, then run `/ship` before re-opening

**If CONTRACT:**
- Identify the writer and the reader that have drifted
- Fix the drift at the source (the factory function in `tests/integration/fixtures.py`
  if applicable)
- Never fix a contract failure by adjusting the test to match wrong behavior
- Fix, then run `/ship` before re-opening

**If GOVERNANCE:**
- Identify which CLAUDE.md rule was violated
- Fix the PR description or commit message
- Do not change code to satisfy a governance failure

## Step 4 — PR DESCRIPTION UPDATE
Before re-pushing, add a section to the PR description:

**PR Failure Report:**
- Failure class: [INFRASTRUCTURE / LOGIC / CONTRACT / GOVERNANCE]
- Root cause:
- Fix applied:
- No-weakening assertion added: [yes / no / not applicable]
- Confirmation the failure no longer occurs:

The goal is never "make CI green." The goal is to strengthen the governed system.
