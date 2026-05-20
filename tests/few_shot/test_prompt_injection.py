"""Phase 3P prompt-injection tests.

The canonical prompt file carries the Few-Shot Examples section between
``FEW_SHOT_BLOCK_BEGIN`` / ``FEW_SHOT_BLOCK_END`` markers. The CLI
flag controls whether the section survives at runtime via
``inject_or_strip_few_shot``. The negative-patterns section is NOT
marker-wrapped and must appear regardless of the flag.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from spectrum_systems_core.few_shot import (
    FEW_SHOT_BEGIN_MARKER,
    FEW_SHOT_END_MARKER,
    inject_or_strip_few_shot,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def test_canonical_prompt_contains_section_and_markers() -> None:
    text = _read_prompt()
    assert FEW_SHOT_BEGIN_MARKER in text
    assert FEW_SHOT_END_MARKER in text
    assert "# Few-Shot Examples (additive)" in text
    assert "Implicit / Guidance-Phrased Decision" in text


def test_negative_patterns_section_present_in_canonical() -> None:
    text = _read_prompt()
    assert "# Do Not Extract (additive)" in text
    assert "Pattern 1: Rhetorical questions" in text
    assert "Pattern 5: Discussion without resolution" in text


def test_few_shot_stripped_when_disabled() -> None:
    text = _read_prompt()
    out = inject_or_strip_few_shot(text, enable=False)
    assert "Few-Shot Examples" not in out
    assert "Implicit / Guidance-Phrased Decision" not in out
    assert FEW_SHOT_BEGIN_MARKER not in out
    assert FEW_SHOT_END_MARKER not in out
    # Negative patterns survive regardless of the flag.
    assert "# Do Not Extract (additive)" in out
    assert "Pattern 1: Rhetorical questions" in out


def test_few_shot_present_when_enabled() -> None:
    text = _read_prompt()
    out = inject_or_strip_few_shot(text, enable=True)
    assert "Few-Shot Examples" in out
    # The implicit-decision example text must be present so the model
    # actually sees the recency-biased pattern.
    assert "Implicit / Guidance-Phrased Decision" in out
    assert "our guidance" in out.lower()
    # Markers themselves are stripped from the rendered prompt so the
    # text the model receives does not include the HTML comments.
    assert FEW_SHOT_BEGIN_MARKER not in out
    assert FEW_SHOT_END_MARKER not in out


def test_prompt_hash_differs_between_enabled_and_disabled() -> None:
    text = _read_prompt()
    enabled = inject_or_strip_few_shot(text, enable=True)
    disabled = inject_or_strip_few_shot(text, enable=False)
    h_on = hashlib.sha256(enabled.encode("utf-8")).hexdigest()
    h_off = hashlib.sha256(disabled.encode("utf-8")).hexdigest()
    assert h_on != h_off, "few-shot flag must change the prompt content hash"


def test_negative_patterns_section_present_when_disabled() -> None:
    """The negative-patterns section is a precision guard and must
    survive when --enable-few-shot is OFF."""
    text = _read_prompt()
    out = inject_or_strip_few_shot(text, enable=False)
    assert "# Do Not Extract (additive)" in out
    for i in range(1, 6):
        assert f"Pattern {i}:" in out


def test_inject_strip_idempotent_when_no_markers() -> None:
    """If the markers are missing the function returns the text
    unchanged. That is the rollback path: a future operator removes
    the section, and the flag becomes a no-op."""
    text = "no markers here\n"
    assert inject_or_strip_few_shot(text, enable=True) == text
    assert inject_or_strip_few_shot(text, enable=False) == text
