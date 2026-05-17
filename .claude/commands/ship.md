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
Run the compliance check from CLAUDE.md:

    python - <<'PY'
    import pathlib, re, subprocess, sys

    scripts_dir = pathlib.Path("scripts")
    test_dirs = [pathlib.Path("tests/integration"), pathlib.Path("tests/scripts")]
    ARTIFACT_TYPE_PATTERN = re.compile(
        r"validate_artifact|meeting_extraction|correction_candidate|"
        r"ground_truth_pair|human_review|decision_few_shot_examples"
    )
    READS_JSON_PATTERN = re.compile(r"json\.loads?\b|json\.load\(|read_text\(")
    try:
        diff = subprocess.check_output(
            ["git", "diff", "--name-only", "origin/main", "--", "scripts/"], text=True
        )
        touched = {pathlib.Path(p).name for p in diff.splitlines() if p.endswith(".py")}
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "scripts/"], text=True
        )
        touched.update(
            pathlib.Path(p).name for p in untracked.splitlines() if p.endswith(".py")
        )
    except subprocess.CalledProcessError:
        touched = {p.name for p in scripts_dir.glob("*.py")}
    coverage_text = "\n".join(
        p.read_text() for d in test_dirs if d.is_dir() for p in d.glob("test_*.py")
    )
    missing = [
        s.name for s in sorted(scripts_dir.glob("*.py"))
        if s.name in touched
        and ARTIFACT_TYPE_PATTERN.search(s.read_text())
        and READS_JSON_PATTERN.search(s.read_text())
        and s.stem not in coverage_text
    ]
    if missing:
        print(f"MISSING integration tests for: {missing}")
        sys.exit(1)
    print("OK: all artifact-reading scripts touched in this PR have integration tests")
    PY

Also run: `python scripts/_gitignore_audit.py`

Both must exit 0. If either fails, fix before proceeding.

## Step 7 — OPEN PR
PR description MUST include, in order:
- What the change does (one paragraph)
- Output of `pytest` (copy-paste)
- Output of the targeted reproduction script (copy-paste)
- What each self-review pass found
- What was fixed in response
- Confirmation the simulated failure case now passes

Do not open the PR if any section is missing.
