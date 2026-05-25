"""Pin the Karpathy behavioral principles section in CLAUDE.md.

These tests assert the structural contract of the Behavioral
Principles section: the four principle headers, the
Spectrum-Systems-specific pre-PR checks, and the placement (the
section must appear before the next governance section so that
behavioral guidance is read before implementation guidance).

A future edit that drops a principle header, removes one of the
prescribed pre-PR grep commands, or moves the section after
``## Governing documents`` will fail these tests.
"""
from __future__ import annotations

import pathlib

CLAUDE_MD = pathlib.Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _read() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def test_think_before_coding_section_present() -> None:
    assert "### 1. Think Before Coding" in _read()


def test_simplicity_first_section_present() -> None:
    assert "### 2. Simplicity First" in _read()


def test_surgical_changes_section_present() -> None:
    assert "### 3. Surgical Changes" in _read()


def test_goal_driven_execution_section_present() -> None:
    assert "### 4. Goal-Driven Execution" in _read()


def test_spectrum_systems_checks_section_present() -> None:
    assert "### Spectrum Systems pre-PR checks" in _read()


def test_schema_field_name_check_script_present() -> None:
    """Principle 1's PR #247 example must spell out the correct field names."""
    body = _read()
    assert "`action`" in body and "`owner`" in body
    assert "`text`" in body and "`assignee`" in body


def test_array_type_completeness_check_script_present() -> None:
    """Pre-PR check #2 must include the source_turns audit one-liner."""
    body = _read()
    assert "source_turns" in body
    assert "meeting_minutes.schema.json" in body
    assert "MISSING source_turns" in body


def test_artifact_kind_grep_present() -> None:
    """Pre-PR check #3 must include the artifact_kind grep."""
    body = _read()
    assert 'grep -rn "artifact_kind" src/ scripts/ tests/' in body


def test_model_string_discipline_check_present() -> None:
    """Pre-PR check #4 must include the model-string grep."""
    body = _read()
    assert "claude-haiku" in body
    assert "claude-sonnet" in body
    assert "claude-opus" in body
    assert "model_id" in body


def test_principles_appear_before_governing_documents() -> None:
    """Behavioral guidance must be read before downstream governance.

    The task description used the placeholder section header
    ``## Pipeline Invariants``; the actual next governance section in
    this repo is ``## Governing documents``. The invariant is the same:
    behavioral principles come first.
    """
    body = _read()
    principles_idx = body.index("## Behavioral Principles (Karpathy)")
    governing_idx = body.index("## Governing documents")
    assert principles_idx < governing_idx
