"""Constraint compliance: paths the in-flight PR must NOT modify.

This test was introduced in Phase 4 as a per-PR constraint guard;
each subsequent phase updates the constrained list so the test
continues to defend the CURRENT phase's "no scope creep" boundary.

Phase 5 (Sonnet model wiring + three-way comparison measurement)
deliberately allows changes to:

- ``scripts/compare_opus_haiku.py`` — the Step 5.6 audit fixes
  (prompt_variant defaulting, `sonnet_prompt_variant` stamp, schema
  drift handling). Each fix is bounded; the audit report enumerates
  the diff.
- ``src/spectrum_systems_core/pipeline/governed_run.py`` — adds
  ``prompt_variant`` to ``ExtractionConfig`` (additive).
- ``src/spectrum_systems_core/schemas/meeting_minutes.schema.json``
  — adds the optional ``extraction_config.prompt_variant`` enum
  field (additive, backward-compatible).

Phase 5 explicitly FORBIDS changes to:

- The Haiku prompt
  (``src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md``)
- The Opus prompt
  (``src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md``;
  Phase 4a creates this file)
- ``scripts/correction_miner.py`` core miner logic
- ``src/spectrum_systems_core/grounding/`` (Phase 1)
- ``src/spectrum_systems_core/glossary/`` (Phase 2P / 3)
- ``src/spectrum_systems_core/transcript_quality/`` (Phase 2R)
- ``src/spectrum_systems_core/few_shot/`` (Phase 3P, if present)

The test is intentionally conservative: when the diff is unavailable
(forked PR sandbox, missing remote), the test skips. The PR's
reviewer-side enforcement is the `verify_rollback_contracts.py`
script plus the explicit constraint section in the Phase 5 rollback
contract entry.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Phase 5 constraint list. See module docstring for the rationale.
CONSTRAINED_PREFIXES: tuple[str, ...] = (
    "src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md",
    "src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md",
    "scripts/correction_miner.py",
    "src/spectrum_systems_core/grounding/",
    "src/spectrum_systems_core/glossary/",
    "src/spectrum_systems_core/transcript_quality/",
    "src/spectrum_systems_core/few_shot/",
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


def test_no_constrained_path_modified() -> None:
    files = _changed_files()
    if files is None:
        pytest.skip("git diff vs origin/main unavailable in this environment")
    offenders = []
    for f in files:
        for prefix in CONSTRAINED_PREFIXES:
            if f == prefix or f.startswith(prefix):
                offenders.append((f, prefix))
                break
    assert not offenders, (
        f"Phase 5 must not modify constrained paths. Offenders: "
        f"{offenders}"
    )
