#!/usr/bin/env python3
"""Integration-test coverage check for artifact-reading scripts.

Enforces the non-negotiable rule from CLAUDE.md / .claude/integration_test_requirement.md:

    Every script under ``scripts/`` that reads a pipeline artifact MUST have
    a contract test under ``tests/integration/`` (preferred) or
    ``tests/scripts/`` (historical) referencing the script stem.

A script "reads an artifact" only when it BOTH:

  1. mentions a known artifact type (``ARTIFACT_TYPE_PATTERN``), AND
  2. actually parses JSON off disk (``READS_JSON_PATTERN``).

Pure seeders / migrators that emit ``artifact_type`` strings but never read
existing files are excluded.

Scope: only scripts TOUCHED in the current PR (vs ``origin/main``) plus any
untracked ``scripts/*.py``. Pre-existing scripts that never had contract
coverage are tech debt; this gate guards new and modified scripts only.

Exit codes:
  0 = all touched artifact-reading scripts have contract coverage
  1 = at least one is missing a contract test

Pure stdlib; runs in any checkout.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TEST_DIRS = (
    REPO_ROOT / "tests" / "integration",
    REPO_ROOT / "tests" / "scripts",
)

ARTIFACT_TYPE_PATTERN = re.compile(
    r"validate_artifact|meeting_extraction|"
    r"correction_candidate|ground_truth_pair|human_review|"
    r"decision_few_shot_examples"
)
READS_JSON_PATTERN = re.compile(r"json\.loads?\b|json\.load\(|read_text\(")


def _touched_scripts() -> set[str]:
    """Scripts modified vs origin/main, plus untracked scripts."""
    try:
        # Compare working tree against origin/main so uncommitted changes count.
        diff = subprocess.check_output(
            ["git", "diff", "--name-only", "origin/main", "--", "scripts/"],
            text=True,
            cwd=REPO_ROOT,
        )
        touched = {
            pathlib.Path(p).name for p in diff.splitlines() if p.endswith(".py")
        }
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "scripts/"],
            text=True,
            cwd=REPO_ROOT,
        )
        touched.update(
            pathlib.Path(p).name for p in untracked.splitlines() if p.endswith(".py")
        )
        return touched
    except subprocess.CalledProcessError:
        # origin/main unavailable (e.g. shallow clone) → check every script.
        return {p.name for p in SCRIPTS_DIR.glob("*.py")}


def _coverage_text() -> str:
    parts: list[str] = []
    for d in TEST_DIRS:
        if d.is_dir():
            for p in d.glob("test_*.py"):
                parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def main() -> int:
    if not SCRIPTS_DIR.is_dir():
        print("OK: no scripts/ directory in this checkout")
        return 0

    touched = _touched_scripts()
    coverage_text = _coverage_text()

    missing: list[str] = []
    for script in sorted(SCRIPTS_DIR.glob("*.py")):
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
        return 1

    print("OK: all artifact-reading scripts touched in this PR have integration tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
