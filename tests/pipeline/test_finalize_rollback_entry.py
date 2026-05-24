"""Tests for scripts/finalize_rollback_entry.py.

The script automates the recurring post-PR-open step of rewriting
`PR #TBD` placeholders in a freshly added `rollback_contracts.md`
entry with the actual PR number. Failure to do so makes the
`verify-rollback-contracts` CI check fail on the first PR run after
open, which has happened on at least five PRs in a row.

These tests pin three properties:

1. ``finalize`` rewrites every ``PR #TBD`` on lines this branch
   added.
2. ``finalize`` leaves pre-existing ``PR #TBD`` strings alone — they
   belong to entries that other (already-merged) PRs documented.
3. ``finalize`` is idempotent: a second run on the already-finalized
   file is a no-op and exits 0.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import finalize_rollback_entry as fre  # noqa: E402


_PRE_EXISTING_ENTRY = (
    "## Phase 2.C schema fixes (PR #TBD)\n\n"
    "### What this change adds\n"
    "- Pre-existing entry that historically uses PR #TBD; the\n"
    "  finalize script must NOT touch this.\n"
    "\n---\n"
)

_NEW_ENTRY = (
    "## agenda_item.summary (PR #TBD)\n\n"
    "### What this change adds\n"
    "- A new optional field on agenda_item.\n"
    "\n### Cross-PR dependency\n"
    "`depends_on`: PR #182 and the Phase 2.C entry (PR #TBD above).\n"
    "\n---\n"
)


def _git(args: list[str], cwd: Path) -> None:
    """Run git with signing disabled so the test works in any env."""
    subprocess.check_call(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"] + args,
        cwd=cwd,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialize a tiny git repo with a base commit on main."""
    _git(["init", "-q", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    _git(["config", "commit.gpgsign", "false"], cwd=tmp_path)
    contracts = tmp_path / "docs" / "architecture" / "rollback_contracts.md"
    contracts.parent.mkdir(parents=True)
    contracts.write_text(_PRE_EXISTING_ENTRY, encoding="utf-8")
    _git(["add", "."], cwd=tmp_path)
    _git(["commit", "-q", "-m", "base"], cwd=tmp_path)
    return contracts


def _add_new_entry_on_branch(repo: Path, contracts: Path) -> None:
    _git(["checkout", "-q", "-b", "feature"], cwd=repo)
    contracts.write_text(
        _PRE_EXISTING_ENTRY + _NEW_ENTRY, encoding="utf-8"
    )
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "add new entry"], cwd=repo)


def test_finalize_rewrites_only_added_heading_lines(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path
    contracts = _make_repo(repo)
    _add_new_entry_on_branch(repo, contracts)
    monkeypatch.setattr(fre, "REPO_ROOT", repo)

    rc = fre.finalize(241, contracts_file=contracts, commit=False)
    assert rc == 0

    after = contracts.read_text(encoding="utf-8")
    # New entry's heading PR # is rewritten:
    assert "## agenda_item.summary (PR #241)" in after
    # Cross-PR BODY line in the NEW entry references a pre-existing
    # `PR #TBD` entry by name — it is intentionally NOT rewritten so
    # the cross-link stays accurate.
    assert "Phase 2.C entry (PR #TBD above)" in after
    # Pre-existing entry heading (was on main before the branch)
    # untouched:
    assert "## Phase 2.C schema fixes (PR #TBD)" in after


def test_finalize_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path
    contracts = _make_repo(repo)
    _add_new_entry_on_branch(repo, contracts)
    monkeypatch.setattr(fre, "REPO_ROOT", repo)

    assert fre.finalize(241, contracts_file=contracts, commit=False) == 0
    first = contracts.read_text(encoding="utf-8")
    # Second run is a no-op:
    assert fre.finalize(241, contracts_file=contracts, commit=False) == 0
    assert contracts.read_text(encoding="utf-8") == first


def test_finalize_noop_when_no_new_entry(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path
    contracts = _make_repo(repo)
    monkeypatch.setattr(fre, "REPO_ROOT", repo)
    # No branch, no new entry — finalize is a no-op:
    assert fre.finalize(241, contracts_file=contracts, commit=False) == 0
    assert contracts.read_text(encoding="utf-8") == _PRE_EXISTING_ENTRY


def test_finalize_missing_file_exits_one(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(fre, "REPO_ROOT", tmp_path)
    missing = tmp_path / "does_not_exist.md"
    assert fre.finalize(241, contracts_file=missing, commit=False) == 1


def test_finalize_with_commit_creates_followup_commit(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path
    contracts = _make_repo(repo)
    _add_new_entry_on_branch(repo, contracts)
    monkeypatch.setattr(fre, "REPO_ROOT", repo)

    before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, cwd=repo
    ).strip()
    assert fre.finalize(241, contracts_file=contracts, commit=True) == 0
    after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, cwd=repo
    ).strip()
    assert before != after
    msg = subprocess.check_output(
        ["git", "log", "-1", "--format=%s"], text=True, cwd=repo
    ).strip()
    assert "finalize PR #241" in msg
