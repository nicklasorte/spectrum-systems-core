"""Phase 2 — gate enforcing that every PR documents its rollback path.

CLAUDE.md (Rollback Contracts section) says every PR that touches a
schema, a gate, or a diagnostic artifact MUST add an entry to
``docs/architecture/rollback_contracts.md`` BEFORE merging. This
script is the CI enforcement so a PR that omits the entry fails fast
instead of relying on reviewer memory.

Checks performed against the PR-under-review:

1. ``rollback_contracts.md`` contains an entry referencing the PR
   number (e.g. ``(PR #197)`` or `PR #197`).
2. The entry mentions at least one file path that is in the PR's
   changed-files list.
3. The entry includes a ``verification_command`` from the whitelist.

Whitelist:

* ``pytest <path>``
* ``python scripts/<name>.py``
* ``python -m spectrum_systems_core.<module>``

Anything else fails the check.

The script is invoked from CI as:

    python scripts/verify_rollback_contracts.py --pr ${PR_NUMBER}

It also accepts:

* ``--changed-files <a,b,c>`` — explicit changed-files list (used by
  the integration tests so the script does not need network access).
* ``--contracts-file <path>`` — explicit contracts file path (the
  integration tests pass a synthetic path).

Exit code 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = REPO_ROOT / "docs" / "architecture" / "rollback_contracts.md"

# Patterns the script trusts as a `verification_command`. A line is
# considered a verification command if it starts with one of these
# tokens AFTER any leading shell prompt / markdown code fence stripping.
_VERIFY_WHITELIST = (
    re.compile(r"^pytest(\s+\S+)+\s*$"),
    re.compile(r"^python\s+scripts/\S+\.py(\s+.*)?$"),
    re.compile(r"^python\s+-m\s+spectrum_systems_core\.\S+(\s+.*)?$"),
)


class RollbackCheckError(RuntimeError):
    pass


def _strip_code_fence_marker(line: str) -> str:
    """Strip a leading ``$ `` shell-prompt sigil or markdown indent."""
    s = line.strip()
    # The contracts file often has lines like "    $ pytest ..." inside
    # fenced blocks; strip a leading "$ ".
    if s.startswith("$ "):
        s = s[2:].strip()
    return s


def _line_matches_whitelist(line: str) -> bool:
    s = _strip_code_fence_marker(line)
    if not s:
        return False
    return any(rx.match(s) for rx in _VERIFY_WHITELIST)


def find_entry_for_pr(text: str, pr_number: int) -> Optional[str]:
    """Return the slice of ``rollback_contracts.md`` that documents PR.

    Entries are delimited by ``---`` horizontal rules. We walk the
    document section by section and pick the first one whose text
    references the PR number. ``PR #<n>`` and ``(PR #<n>)`` both match.
    """
    sections = re.split(r"\n---\s*\n", text)
    needle_a = f"PR #{pr_number}"
    needle_b = f"(PR #{pr_number})"
    for section in sections:
        if needle_a in section or needle_b in section:
            return section
    return None


def changed_files_in_pr(pr_number: int) -> List[str]:
    """Best-effort changed-files list via git diff vs origin/main.

    When the diff is unavailable (forked PR, missing remote, etc.) we
    fall back to ``git ls-files`` on the working tree — which is over-
    inclusive but never returns nothing. The integration tests pass
    ``--changed-files`` explicitly so this branch never runs in CI.
    """
    try:
        diff = subprocess.check_output(
            ["git", "diff", "--name-only", "origin/main"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=REPO_ROOT,
        )
        files = [ln.strip() for ln in diff.splitlines() if ln.strip()]
        if files:
            return files
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        diff = subprocess.check_output(
            ["git", "ls-files", "--modified", "--others", "--exclude-standard"],
            text=True,
            cwd=REPO_ROOT,
        )
        return [ln.strip() for ln in diff.splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def verify_pr(
    pr_number: int,
    *,
    contracts_file: Path = DEFAULT_CONTRACTS,
    changed_files: Optional[List[str]] = None,
) -> bool:
    """Return True iff every check passes for the given PR.

    Raises :class:`RollbackCheckError` with a precise reason on
    failure. The CLI catches the exception, prints the message, and
    exits with code 1.
    """
    if not contracts_file.is_file():
        raise RollbackCheckError(
            f"rollback_contracts.md not found at {contracts_file}"
        )
    text = contracts_file.read_text(encoding="utf-8")

    entry = find_entry_for_pr(text, pr_number)
    if entry is None:
        raise RollbackCheckError(
            f"no rollback_contracts.md entry references PR #{pr_number}"
        )

    files = changed_files if changed_files is not None else changed_files_in_pr(
        pr_number
    )
    # The entry must mention at least one of the PR's changed files.
    # We match the file basename or relative path appearing anywhere in
    # the section text — the contracts file typically uses repo-rooted
    # paths in code fences (e.g. `src/spectrum_systems_core/foo.py`).
    referenced = [
        f for f in files if (f and (f in entry or Path(f).name in entry))
    ]
    if not referenced:
        raise RollbackCheckError(
            f"PR #{pr_number} entry does not reference any of the "
            f"PR's changed files. Changed: {files}"
        )

    # The entry must include at least one verification_command from
    # the whitelist. We scan inside fenced ```bash blocks if present;
    # otherwise we scan every line.
    has_whitelisted = False
    for raw_line in entry.splitlines():
        if _line_matches_whitelist(raw_line):
            has_whitelisted = True
            break
    if not has_whitelisted:
        raise RollbackCheckError(
            f"PR #{pr_number} entry has no verification_command from the "
            f"whitelist. Allowed forms: `pytest <path>`, "
            f"`python scripts/<name>.py`, "
            f"`python -m spectrum_systems_core.<module>`."
        )

    return True


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument(
        "--contracts-file",
        type=Path,
        default=DEFAULT_CONTRACTS,
    )
    parser.add_argument(
        "--changed-files",
        type=str,
        default=None,
        help="Comma-separated list of changed file paths (overrides git diff).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    explicit_files: Optional[List[str]] = None
    if args.changed_files is not None:
        explicit_files = [
            s.strip() for s in args.changed_files.split(",") if s.strip()
        ]

    try:
        verify_pr(
            args.pr,
            contracts_file=args.contracts_file,
            changed_files=explicit_files,
        )
    except RollbackCheckError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"OK: rollback contract for PR #{args.pr} is complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
