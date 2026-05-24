#!/usr/bin/env python3
"""Finalize a freshly added rollback_contracts.md entry with the real PR #.

Background
----------

`docs/architecture/rollback_contracts.md` entries must reference the
PR number — `.github/workflows/verify-rollback-contracts.yml` looks
for `PR #<n>` or `(PR #<n>)` and fails the PR check when the lookup
returns nothing. The PR number does not exist until `create_pull_request`
returns it, so the pragmatic flow is:

1. Author the entry with `(PR #TBD)` as the heading marker.
2. Push the branch and open the PR via the MCP tool (or `gh pr create`).
3. Read the assigned number back and rewrite `PR #TBD` →
   `PR #<actual>` ONLY in the entry this branch added.
4. Commit + push the finalization in the same session.

Step 3 has been done by hand every time and silently skipped at least
three times in a row, producing a failing `verify` check that then
has to be chased down. This script automates step 3 so the session
never forgets and the `verify` check passes on the first CI run after
PR open.

Behavior
--------

* Rewrites every `PR #TBD` occurrence inside the entries that this
  branch ADDED to `rollback_contracts.md` (lines whose `git diff
  origin/main -- rollback_contracts.md` prefix is `+`) to
  `PR #<actual>`. Pre-existing `PR #TBD` placeholders in entries the
  branch did not touch are left alone — they belong to other,
  already-merged PRs.
* When `--commit` is set, stages the file, creates a follow-up
  commit ("docs(rollback): finalize PR # in agenda_item.summary
  entry"), and prints the commit SHA. Push remains the caller's
  responsibility so this script never surprises the operator.
* Exit code 0 on success (including the no-op case where no `PR
  #TBD` line was added by this branch — finalization is idempotent).
* Exit code 1 only when the contracts file is missing or the diff
  computation fails.

Usage
-----

    python scripts/finalize_rollback_entry.py --pr 241
    python scripts/finalize_rollback_entry.py --pr 241 --commit

The script is intentionally read-only by default so it can run as a
dry-run from any session and the operator can review the diff before
committing.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = REPO_ROOT / "docs" / "architecture" / "rollback_contracts.md"
PLACEHOLDER = "PR #TBD"


def _added_lines_in_diff(contracts_file: Path) -> list[str]:
    """Return the list of lines this branch ADDED to ``contracts_file``.

    Lines are returned without the leading `+`. Hunk headers and the
    `+++` file marker are filtered out. When the diff cannot be
    computed (e.g. no `origin/main`), returns an empty list — the
    caller treats that as "nothing to finalize" rather than crashing.
    """
    for ref in ("origin/main", "main"):
        try:
            out = subprocess.check_output(
                [
                    "git",
                    "diff",
                    f"{ref}...HEAD",
                    "--",
                    str(contracts_file.relative_to(REPO_ROOT)),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=REPO_ROOT,
            )
            if not out:
                continue
            added: list[str] = []
            for line in out.splitlines():
                if line.startswith("+++"):
                    continue
                if line.startswith("+"):
                    added.append(line[1:])
            return added
        except subprocess.CalledProcessError:
            continue
    return []


def _rewrite_added_tbd_placeholders(
    text: str, added_lines: list[str], pr_number: int
) -> tuple[str, int]:
    """Rewrite ``PR #TBD`` → ``PR #<n>`` ONLY on entry-HEADING lines
    this branch added.

    Only `##`-headed lines are rewritten. Body lines that mention
    `PR #TBD` as a textual cross-reference to a pre-existing entry
    are intentionally left alone — those are legitimate references,
    not unfinalized placeholders, and rewriting them would mis-route
    the cross-link.

    The match is line-level so a single branch that adds multiple
    entry headings (rare but possible) finalizes them all in one
    pass. A line that did not change in this branch (lives in
    another, already-merged entry) is also left untouched.

    Returns the new file text plus the number of replacements made.
    """
    if not added_lines:
        return text, 0
    added_with_tbd_headings = {
        ln for ln in added_lines
        if PLACEHOLDER in ln and ln.lstrip().startswith("##")
    }
    if not added_with_tbd_headings:
        return text, 0
    new_lines: list[str] = []
    replacements = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped in added_with_tbd_headings:
            new_line = line.replace(PLACEHOLDER, f"PR #{pr_number}")
            if new_line != line:
                replacements += 1
            new_lines.append(new_line)
        else:
            new_lines.append(line)
    return "".join(new_lines), replacements


def finalize(
    pr_number: int,
    *,
    contracts_file: Path = DEFAULT_CONTRACTS,
    commit: bool = False,
) -> int:
    """Rewrite the contracts file and (optionally) commit the change.

    Returns 0 on success (including the no-op case), 1 on hard errors.
    """
    if not contracts_file.is_file():
        print(
            f"ERROR: rollback_contracts.md not found at {contracts_file}",
            file=sys.stderr,
        )
        return 1
    added = _added_lines_in_diff(contracts_file)
    text = contracts_file.read_text(encoding="utf-8")
    new_text, count = _rewrite_added_tbd_placeholders(text, added, pr_number)
    if count == 0:
        print(
            "No PR #TBD placeholder added by this branch — nothing to "
            f"finalize. (PR #{pr_number} either already finalized or "
            "the branch added no new entry.)"
        )
        return 0
    contracts_file.write_text(new_text, encoding="utf-8")
    print(
        f"Replaced {count} `PR #TBD` placeholder(s) with `PR #{pr_number}` "
        f"in {contracts_file.relative_to(REPO_ROOT)}."
    )
    if commit:
        try:
            subprocess.check_call(
                ["git", "add", str(contracts_file.relative_to(REPO_ROOT))],
                cwd=REPO_ROOT,
            )
            subprocess.check_call(
                [
                    "git",
                    "commit",
                    "-m",
                    f"docs(rollback): finalize PR #{pr_number} in entry",
                ],
                cwd=REPO_ROOT,
            )
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                text=True,
                cwd=REPO_ROOT,
            ).strip()
            print(f"Committed as {sha}. Run `git push` to publish.")
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: commit failed ({exc})", file=sys.stderr)
            return 1
    else:
        print(
            "File written but NOT committed. Re-run with --commit to "
            "create the follow-up commit, or stage manually."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="Assigned PR number")
    parser.add_argument(
        "--contracts-file",
        type=Path,
        default=DEFAULT_CONTRACTS,
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Also create a follow-up commit (push is still manual).",
    )
    args = parser.parse_args(argv)
    return finalize(
        args.pr,
        contracts_file=args.contracts_file,
        commit=args.commit,
    )


if __name__ == "__main__":
    sys.exit(main())
