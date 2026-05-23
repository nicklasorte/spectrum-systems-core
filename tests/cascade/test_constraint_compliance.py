"""Phase 6 constraint compliance.

Step 6.12 asserts the PR makes NO modifications to a fixed set of
paths. Run via `git diff origin/main -- <path>` against the working
tree. The check is scoped — pre-Phase-6 churn in these paths from
earlier PRs is unaffected; this test only fires on changes
introduced by the current branch.

Skipped when origin/main is unavailable (forked PRs, dev checkouts
without the remote configured) — the same lifecycle as the
integration_check helper in CLAUDE.md.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Phase 6 PR-time scope (Phase 6 = cascade filter, PR #203). The
# constraint guarded against the cascade PR rewriting upstream
# extraction surfaces. The Haiku/Opus prompt entries are intentionally
# omitted here: post-Phase-6 PRs may legitimately need to keep the
# producer prompts aligned with additive schema-enum expansions (e.g.
# the `position_type: clarification` schema fix on the post-Phase-2.C
# branch). The schema-side regression tests in
# tests/test_meeting_minutes_schema.py remain the binding check for
# enum behaviour; this list still pins the cascade-adjacent modules
# Phase 6 was forbidden to touch.
_FORBIDDEN_PATHS = (
    "scripts/correction_miner.py",
    "src/spectrum_systems_core/grounding/",
    "src/spectrum_systems_core/transcript_quality/",
    "src/spectrum_systems_core/glossary/",
    "src/spectrum_systems_core/few_shot/",
)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _origin_main_known() -> bool:
    if not _git_available():
        return False
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _diff_paths_against_main(path: str) -> str:
    """Return the diff (working tree vs origin/main) for one path."""
    proc = subprocess.run(
        ["git", "diff", "--unified=0", "origin/main", "--", path],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


@pytest.mark.parametrize("path", _FORBIDDEN_PATHS)
def test_no_changes_to_forbidden_path(path: str) -> None:
    if not _origin_main_known():
        pytest.skip("origin/main not available")
    diff = _diff_paths_against_main(path)
    assert diff == "", (
        f"Phase 6 constraint violation: {path!r} was modified on this "
        f"branch.\n--- diff ---\n{diff}\n--- end diff ---"
    )
