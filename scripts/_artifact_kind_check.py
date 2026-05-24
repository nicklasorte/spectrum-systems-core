#!/usr/bin/env python3
"""Stop-hook check: refuse to end a session that reintroduces ``artifact_kind``.

The constitution mandates the field name ``artifact_type``. The deprecated
name ``artifact_kind`` is allowed only in a TEACHING context — a line
that compares the two names (``... artifact_kind ... instead of ...
artifact_type ...``). This mirrors the exclusion in
``scripts/validate_claude_md.py``.

This check scans every changed-but-uncommitted file (vs ``HEAD``) and
every untracked file. A hit on any line that fails the teaching-context
exclusion is a failure.

Exit codes:
  0 = no offending occurrence
  1 = at least one new ``artifact_kind`` reference outside a teaching context

Pure stdlib.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _changed_files() -> list[pathlib.Path]:
    """Files modified vs HEAD plus untracked files."""
    try:
        diff = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"],
            text=True,
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        )
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            text=True,
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    names = set(diff.splitlines()) | set(untracked.splitlines())
    return [REPO_ROOT / n for n in names if n and (REPO_ROOT / n).is_file()]


_SCRIPT_NAME = pathlib.Path(__file__).name


def _is_teaching_line(line: str) -> bool:
    """A line that mentions both field names, or references this script's
    path, is treated as a teaching / wiring context — not a real
    reintroduction of the deprecated field name."""
    if "artifact_type" in line:
        return True
    if _SCRIPT_NAME in line:
        return True
    return False


# Skip docs; the rule targets code introductions.
_SKIP_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
_SKIP_NAME_FRAGMENT = "artifact_kind"  # this script's own filename


def main() -> int:
    findings: list[str] = []
    for path in _changed_files():
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        if _SKIP_NAME_FRAGMENT in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "artifact_kind" not in text:
            continue
        rel = path.relative_to(REPO_ROOT)
        for i, line in enumerate(text.splitlines(), start=1):
            if "artifact_kind" not in line:
                continue
            if _is_teaching_line(line):
                continue
            findings.append(f"{rel}:{i}: {line.strip()}")

    if findings:
        print(
            "artifact_kind found in changed files outside teaching context — "
            "use artifact_type instead (constitution invariant):"
        )
        for f in findings:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
