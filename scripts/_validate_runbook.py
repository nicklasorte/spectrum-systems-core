"""Validate that every workflow path referenced in the first-run
runbook still exists.

Renaming a workflow file silently breaks the runbook because GitHub
just renders dead links — the operator following the runbook from a
phone won't notice until the dispatch button isn't there. This
script catches that drift before the PR lands.

Usage:

    python scripts/_validate_runbook.py
    python scripts/_validate_runbook.py --runbook docs/runbooks/first_run.md

Exit codes:

    0 — every referenced workflow file exists.
    1 — one or more references are dangling, or the runbook is missing.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import List

# Match ``.github/workflows/<name>.yml``. The character class is
# narrow on purpose so we don't accidentally pick up a ``.yml.bak``
# or a markdown link target like ``...yml#step-1``. Anchored to the
# extension to avoid matching folders.
_WORKFLOW_RE = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.yml")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def extract_workflow_refs(runbook_text: str) -> List[str]:
    """Return the unique workflow paths referenced in the runbook,
    sorted for stable diagnostics. Order does not matter for the
    correctness of the check; sorting just makes the failure
    output reproducible across runs."""
    return sorted(set(_WORKFLOW_RE.findall(runbook_text)))


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runbook",
        default="docs/runbooks/first_run.md",
        help="Path to the runbook (default: docs/runbooks/first_run.md)",
    )
    args = parser.parse_args(argv)

    runbook_path = (REPO_ROOT / args.runbook).resolve()
    if not runbook_path.is_file():
        print(f"ERROR: runbook not found at {runbook_path}", file=sys.stderr)
        return 1

    refs = extract_workflow_refs(
        runbook_path.read_text(encoding="utf-8")
    )
    if not refs:
        # Empty refs is suspicious — a runbook that references zero
        # workflows is either a stub or was malformed by an editor.
        # Refuse to silently pass.
        print(
            f"ERROR: no .github/workflows/*.yml references found in "
            f"{runbook_path}. Either the runbook is empty or the "
            f"reference format changed.",
            file=sys.stderr,
        )
        return 1

    missing: List[str] = []
    for rel in refs:
        if not (REPO_ROOT / rel).is_file():
            missing.append(rel)

    if missing:
        print(
            f"RUNBOOK VALIDATION FAILED: {len(missing)} dangling reference(s):",
            file=sys.stderr,
        )
        for rel in missing:
            print(f"  - {rel}", file=sys.stderr)
        print(
            "Fix: update the runbook to reference the renamed workflow "
            "file, or restore the missing workflow.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: all {len(refs)} workflow reference(s) in {args.runbook} exist.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
