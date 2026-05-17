---
description: Verify integration test coverage for artifact-reading scripts touched in this session. Run before /ship.
---

You are verifying that every artifact-reading script touched in this session has
integration test coverage. Run the following checks in order.

## Check 1 — Script coverage

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
    print("OK: all artifact-reading scripts touched in this session have integration tests")
    PY

## Check 2 — Gitignore audit

    python scripts/_gitignore_audit.py

## Check 3 — Fixture factory compliance
For each artifact-reading script touched, verify:
- The integration test uses `tests/integration/fixtures.py` factory functions
- It does NOT hand-roll dicts
- It writes to a real temp directory (not mocked)
- It calls the script via `subprocess.run`
- It asserts correct output on disk, not just return code
- The script itself calls `scripts/_artifact_validator.validate_artifact`
  before reading any field

List each script and confirm compliance or flag the gap.

## Check 4 — Artifact manifest
If any new artifact type was added or any existing artifact's path or schema changed:
- Confirm `docs/architecture/artifact_manifest.md` has been updated
- Confirm `python scripts/_gitignore_audit.py` passes

State the result of each check explicitly. If any check fails, fix the gap before running `/ship`.
