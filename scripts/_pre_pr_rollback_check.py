#!/usr/bin/env python3
"""Pre-PR check: every commit touching verify-trigger paths must add
a rollback_contracts.md entry on the same branch, AND the entry must
not leave a `PR #TBD` placeholder once a PR exists for the branch.

Background
----------

`.github/workflows/verify-rollback-contracts.yml` runs at PR time and
fails when the changed-files list intersects with the verify-trigger
paths but `docs/architecture/rollback_contracts.md` has no entry
referencing the PR number. The CI check is the LAST line of defense;
this script is the FIRST line — it runs as a Stop hook so Claude
Code can't end a turn that has uncommitted/unpushed changes
violating the contract.

Two failure modes are caught:

1. **Missing entry.** Trigger paths in the diff with zero changes
   to `rollback_contracts.md`. Fail-fast — the operator must add
   the entry.
2. **Unfinalized PR number.** An entry exists but a line this
   branch added still contains `PR #TBD`, AND the branch has been
   pushed (i.e. the remote tracking branch exists). When the
   remote exists, the PR is either open or imminent; the entry
   must reference the real number before merge or the CI check
   fails. The fix is one command:
   `python scripts/finalize_rollback_entry.py --pr <N> --commit`.
   This second mode is the recurring failure the user surfaced
   after PRs #235, #236, #237, #240, and #241.

Exit codes:
  0 = no trigger paths in diff, OR diff includes a finalized
      rollback_contracts.md modification.
  1 = either failure mode above. Halt — Claude must resolve
      before ending the turn.

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
TBD_PLACEHOLDER = "PR #TBD"


def _branch_pushed_to_remote() -> bool:
    """True if the current local branch has an upstream tracking branch.

    A pushed branch is the trigger for the second failure mode: once
    the branch has been published, GitHub has (or imminently will)
    assigned a PR number, and any remaining `PR #TBD` placeholder
    must be finalized before the next push or the CI check breaks.
    Pre-push iterations are allowed to keep `PR #TBD` because the
    real number is not yet knowable.
    """
    try:
        subprocess.check_call(
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=REPO_ROOT,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _added_tbd_lines_in_rollback() -> list[str]:
    """Return ENTRY-HEADING lines this branch ADDED to
    ``rollback_contracts.md`` that still contain the ``PR #TBD``
    placeholder.

    Only `##`-headed lines are considered. Body lines that mention
    `PR #TBD` as a textual cross-reference to a pre-existing entry
    (e.g. "Phase 2.C entry (PR #TBD above; pre-existing entry)")
    are intentionally NOT flagged — those are legitimate references,
    not unfinalized placeholders.

    Only the `+` lines from `git diff origin/main...HEAD` are
    inspected so pre-existing `PR #TBD` headings in already-merged
    entries do not trip the check.
    """
    rollback_path = REPO_ROOT / ROLLBACK_CONTRACTS_PATH
    if not rollback_path.is_file():
        return []
    for ref in ("origin/main", "main"):
        try:
            out = subprocess.check_output(
                [
                    "git",
                    "diff",
                    f"{ref}...HEAD",
                    "--",
                    ROLLBACK_CONTRACTS_PATH,
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=REPO_ROOT,
            )
            if not out:
                continue
            offending: list[str] = []
            for line in out.splitlines():
                if line.startswith("+++"):
                    continue
                if not line.startswith("+"):
                    continue
                body = line[1:]
                stripped = body.lstrip()
                if not stripped.startswith("##"):
                    continue
                if TBD_PLACEHOLDER in body:
                    offending.append(body)
            return offending
        except subprocess.CalledProcessError:
            continue
    return []


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

    # Failure mode 2: unfinalized PR #TBD on a branch that has been
    # pushed. Catch even when the trigger-path list is empty so a
    # follow-up commit that only touches rollback_contracts.md is
    # still validated.
    if _branch_pushed_to_remote():
        offending = _added_tbd_lines_in_rollback()
        if offending:
            print(
                "ROLLBACK CONTRACT NOT FINALIZED:\n"
                "  The branch has been pushed (upstream tracking branch\n"
                "  exists) and rollback_contracts.md still contains a\n"
                "  `PR #TBD` placeholder on a line this branch added:",
                file=sys.stderr,
            )
            for line in offending[:5]:
                print(f"    {line.rstrip()}", file=sys.stderr)
            print(
                "\n"
                "  The verify-rollback-contracts CI check looks for\n"
                "  `PR #<n>` or `(PR #<n>)` and FAILS on `PR #TBD`.\n"
                "\n"
                "  Fix:\n"
                "    1. Find the assigned PR number (the URL returned\n"
                "       by `create_pull_request` or `gh pr view`).\n"
                "    2. Run:\n"
                "         python scripts/finalize_rollback_entry.py \\\n"
                "           --pr <N> --commit\n"
                "    3. `git push` the follow-up commit.\n"
                "\n"
                "  The finalize script rewrites only the lines this\n"
                "  branch added, so pre-existing `PR #TBD` entries\n"
                "  belonging to other PRs are left alone.",
                file=sys.stderr,
            )
            return 1

    # Failure mode 1: trigger paths in diff, rollback_contracts.md
    # untouched on this branch.
    if not triggered:
        return 0
    if ROLLBACK_CONTRACTS_PATH in changed:
        return 0
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
        "  After pushing and opening the PR, run\n"
        "  `python scripts/finalize_rollback_entry.py --pr <N> --commit`\n"
        "  to rewrite the entry's `PR #TBD` placeholder with the\n"
        "  assigned PR number, then push the follow-up commit.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
