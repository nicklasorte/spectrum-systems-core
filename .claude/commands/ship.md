---
description: Required pre-PR loop — build, self-review, fix, verify, re-review. Run before every PR.
---

You are preparing to open a PR. Do not open the PR until every step below is complete and documented. No exceptions.

## Step 1 — BUILD
Implement the change. When done, list every file written or modified.

## Step 2 — SELF-REVIEW
Re-read every file you wrote. Attack it. Look specifically for:
- Any path where a bad artifact could pass a gate silently
- Any gate bypassable by missing input
- Any failure a new engineer could not explain from the artifact alone
- Any field using `artifact_kind` instead of `artifact_type`
- Any step that only fails post-CI instead of pre-PR

List every finding. If you find nothing, say so explicitly and explain why.

## Step 3 — FIX
Fix every finding from Step 2. List what was fixed and why.

## Step 4 — VERIFY
Run the following. Copy the actual output — do not paraphrase.

1. Full test suite: `python -m pytest`
2. A targeted script that reproduces the specific failure this change addresses,
   then asserts it no longer occurs. Write this script inline. Run it. Show the output.
3. Assert no regression on related paths.

If you cannot write a targeted reproduction script, stop and explain why before proceeding.

## Step 5 — RE-REVIEW
Re-read the fixed code. Attack it again. List any new findings. Fix them.

## Step 6 — INTEGRATION TEST CHECK
Run both compliance checks:

    python scripts/_integration_test_check.py
    python scripts/_gitignore_audit.py

Both must exit 0. If either fails, fix before proceeding.

See `.claude/integration_test_requirement.md` and
`.claude/artifact_manifest_requirement.md` for the underlying rules.

## Step 7 — OPEN PR
PR description MUST include, in order:
- What the change does (one paragraph)
- Output of `pytest` (copy-paste)
- Output of the targeted reproduction script (copy-paste)
- What each self-review pass found
- What was fixed in response
- Confirmation the simulated failure case now passes

Do not open the PR if any section is missing.
