"""Phase 4 — constraint compliance test.

The Phase 4 spec enumerates a list of constrained paths the PR must
NOT modify. This test runs ``git diff --name-only`` against
``origin/main`` (the same diff base the CI uses) and asserts that no
constrained path appears.

The check is intentionally conservative: when the diff is
unavailable (forked PR sandbox, missing remote), the test skips. The
PR's reviewer-side enforcement is the `verify_rollback_contracts.py`
script plus the explicit constraint section in the Phase 4 rollback
contract entry.

Scoping: this test only applies to PRs that are themselves Phase 4
work (i.e. that introduce or modify files under
``src/spectrum_systems_core/corpus/`` or ``data/corpus/``). Branches
that build on top of merged Phase 4 (e.g. a later Phase 3P PR that
legitimately touches ``pipeline/governed_run.py``) skip — the
constrained-path rule binds Phase 4 itself, not every subsequent PR.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

CONSTRAINED_PREFIXES: tuple[str, ...] = (
    "src/spectrum_systems_core/pipeline/governed_run.py",
    "src/spectrum_systems_core/schemas/meeting_minutes.schema.json",
    "scripts/compare_opus_haiku.py",
    "scripts/correction_miner.py",
    "src/spectrum_systems_core/grounding/",
    "src/spectrum_systems_core/glossary/",
    "src/spectrum_systems_core/transcript_quality/",
)

# Markers that identify a PR as Phase 4 work. If none of these appear
# in the diff, the constraint check does not apply.
PHASE_4_MARKERS: tuple[str, ...] = (
    "src/spectrum_systems_core/corpus/",
    "data/corpus/",
)


def _changed_files() -> list[str] | None:
    """Return the list of changed paths vs origin/main, or None when
    the diff cannot be computed (forked PR / no remote)."""
    repo_root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "origin/main"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return files


def _is_phase_4_branch(files: list[str]) -> bool:
    """True iff the diff touches a Phase-4 marker path."""
    for f in files:
        for marker in PHASE_4_MARKERS:
            if f == marker or f.startswith(marker):
                return True
    return False


def test_no_constrained_path_modified() -> None:
    files = _changed_files()
    if files is None:
        pytest.skip("git diff vs origin/main unavailable in this environment")
    if not _is_phase_4_branch(files):
        pytest.skip(
            "constraint check applies to Phase 4 PRs only (no Phase 4 marker "
            "paths in this diff)"
        )
    offenders = []
    for f in files:
        for prefix in CONSTRAINED_PREFIXES:
            if f == prefix or f.startswith(prefix):
                offenders.append((f, prefix))
                break
    assert not offenders, (
        f"Phase 4 must not modify constrained paths. Offenders: "
        f"{offenders}"
    )
