#!/usr/bin/env python3
"""Pre-PR check: every commit touching verify-trigger paths must add
a rollback_contracts.md entry on the same branch.

Background
----------

`.github/workflows/verify-rollback-contracts.yml` runs at PR time and
fails when the changed-files list intersects with the verify-trigger
paths but `docs/architecture/rollback_contracts.md` has no entry
referencing the PR number. The CI check is the LAST line of defense;
this script is the FIRST line — it runs as a Stop hook so Claude
Code can't end a turn that has uncommitted/unpushed changes
violating the contract.

The script intentionally cannot check the PR-number reference (no
PR exists pre-push). What it CAN check, fail-fast, is the much more
common gap that has now happened three PRs in a row: changes to
trigger paths landed on the branch with ZERO modifications to
`rollback_contracts.md`. The PR-number reference still has to be
added before opening the PR — but at least an entry now exists.

Exit codes:
  0 = no trigger paths in diff, OR diff includes a
      rollback_contracts.md modification (the entry exists; PR
      number may still need verification at PR time).
  1 = trigger paths in diff and rollback_contracts.md untouched
      on this branch. Halt — Claude must add the entry before
      ending the turn.

The script is silent on success so it doesn't add noise to every
Stop event during normal work. On failure it prints exactly what
the operator needs to do.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths whose modification triggers the CI verify-rollback-contracts
# check. MUST stay in sync with
# `.github/workflows/verify-rollback-contracts.yml` (the workflow's
# `on.pull_request.paths` block). Drift here means this script
# greenlights diffs that the CI check rejects — defeating the point.
TRIGGER_PATH_PREFIXES = (
    "src/spectrum_systems_core/schemas/",
    "src/spectrum_systems_core/pipeline/",
    "src/spectrum_systems_core/calibration/",
    "src/spectrum_systems_core/promotion/",
    "scripts/verify_rollback_contracts.py",
)

ROLLBACK_CONTRACTS_PATH = "docs/architecture/rollback_contracts.md"


def _changed_files_vs_main() -> list[str]:
    """Return the list of files that differ between HEAD and origin/main.

    Falls back to the merge-base-against-main when origin/main is not
    fetched. Returns an empty list when no diff can be computed (e.g.
    fresh repo with no main yet) — that case is treated as "nothing
    to check" rather than a failure.
    """
    for ref in ("origin/main", "main"):
        try:
            out = subprocess.check_output(
                ["git", "diff", "--name-only", f"{ref}...HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=REPO_ROOT,
            )
            files = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if files:
                # Also pick up uncommitted-but-staged changes so a
                # half-completed entry is detected before commit.
                uncommitted = subprocess.check_output(
                    ["git", "diff", "--name-only", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    cwd=REPO_ROOT,
                ).splitlines()
                files.extend(ln.strip() for ln in uncommitted if ln.strip())
                return list(dict.fromkeys(files))
        except subprocess.CalledProcessError:
            continue
    return []


def _is_trigger_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in TRIGGER_PATH_PREFIXES)


def main() -> int:
    changed = _changed_files_vs_main()
    if not changed:
        return 0
    triggered = [p for p in changed if _is_trigger_path(p)]
    if not triggered:
        return 0
    if ROLLBACK_CONTRACTS_PATH in changed:
        return 0
    # Trigger paths in diff, rollback_contracts.md untouched. Halt.
    print(
        "ROLLBACK CONTRACT MISSING:\n"
        "  This branch touches verify-trigger paths but does NOT\n"
        "  modify docs/architecture/rollback_contracts.md.\n"
        "\n"
        "  The verify-rollback-contracts CI check will FAIL on the PR.\n"
        "\n"
        "  Trigger-path changes detected:",
        file=sys.stderr,
    )
    for p in sorted(triggered):
        print(f"    - {p}", file=sys.stderr)
    print(
        "\n"
        "  Fix: append a new entry to docs/architecture/rollback_contracts.md\n"
        "  before opening the PR. Match the format of the Phase 3.A or\n"
        "  Phase 3.B-E entries (sections: What this change adds / To roll\n"
        "  back / Data migration / Verification / verification_command /\n"
        "  Cross-PR dependency / Operator action after merge).\n"
        "\n"
        "  After pushing, also confirm the entry's 'PR #<number>' string\n"
        "  matches the actual PR number — the CI check requires both an\n"
        "  entry AND the right PR number to clear.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
