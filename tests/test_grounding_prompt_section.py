"""Tests for the Phase 4.A VERBATIM SOURCE GROUNDING prompt section.

The section MUST be byte-identical between the Haiku LLM prompt and
the Opus reference-baseline prompt — they drive different models but
the grounding contract is shared, so any drift between the two would
silently surface as model-attributable F1 noise rather than the prompt
divergence it is.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
)
LLM_PROMPT = PROMPTS_DIR / "meeting_minutes_llm.md"
OPUS_PROMPT = PROMPTS_DIR / "meeting_minutes_opus.md"

SECTION_HEADER = "## VERBATIM SOURCE GROUNDING (REQUIRED)"

# The next section break after VERBATIM SOURCE GROUNDING in both prompts
# is "## Reason field". The section is everything between them.
SECTION_END = "## Reason field"


def _extract_section(text: str) -> str:
    start = text.index(SECTION_HEADER)
    end = text.index(SECTION_END, start)
    return text[start:end].rstrip("\n") + "\n"


def test_verbatim_grounding_section_present_in_llm_prompt() -> None:
    assert SECTION_HEADER in LLM_PROMPT.read_text(encoding="utf-8")


def test_verbatim_grounding_section_present_in_opus_prompt() -> None:
    assert SECTION_HEADER in OPUS_PROMPT.read_text(encoding="utf-8")


def test_verbatim_grounding_section_byte_identical() -> None:
    """Both prompts MUST carry the same section text — byte-identical."""
    llm = _extract_section(LLM_PROMPT.read_text(encoding="utf-8"))
    opus = _extract_section(OPUS_PROMPT.read_text(encoding="utf-8"))
    assert llm == opus, (
        "VERBATIM SOURCE GROUNDING section drifted between Haiku and "
        "Opus prompts. Diff:\n"
        + "\n".join(
            f"{i}: L={lc!r} O={oc!r}"
            for i, (lc, oc) in enumerate(zip(llm, opus))
            if lc != oc
        )
    )


def test_section_lists_all_14_claim_shaped_types() -> None:
    section = _extract_section(LLM_PROMPT.read_text(encoding="utf-8"))
    expected_types = [
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "claims",
        "risks",
        "cross_references",
        "regulatory_references",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
        "procedural_ruling",
    ]
    for t in expected_types:
        assert t in section, f"{t!r} missing from prompt section"


def test_section_states_10_char_minimum() -> None:
    section = _extract_section(LLM_PROMPT.read_text(encoding="utf-8"))
    # Either "10–1000" (en dash) or "10+" or both — accept any phrasing
    # that includes the literal "10" near "char".
    assert "10" in section
    assert "char" in section.lower()


def test_section_says_reproduce_transcription_errors() -> None:
    section = _extract_section(LLM_PROMPT.read_text(encoding="utf-8"))
    lower = section.lower()
    assert "transcription error" in lower
    assert "as-is" in lower or "as is" in lower


def test_section_says_fail_closed() -> None:
    section = _extract_section(LLM_PROMPT.read_text(encoding="utf-8"))
    assert "fail-closed" in section.lower() or "fail closed" in section.lower()


def test_prompt_section_order_do_not_extract_then_grounding_then_reason() -> None:
    """DO NOT EXTRACT → VERBATIM SOURCE GROUNDING → Reason field."""
    for prompt in (LLM_PROMPT, OPUS_PROMPT):
        text = prompt.read_text(encoding="utf-8")
        i_dne = text.index("## DO NOT EXTRACT")
        i_vsg = text.index(SECTION_HEADER)
        i_reason = text.index("## Reason field")
        assert i_dne < i_vsg < i_reason, (
            f"section order broken in {prompt.name}: "
            f"DO NOT EXTRACT@{i_dne}, VSG@{i_vsg}, Reason@{i_reason}"
        )


def test_prompt_version_bumped_to_4a() -> None:
    """Both prompts MUST declare version 4.A or later in the frontmatter.

    Phase 4.A pinned the version at "4.A"; Phase 4.B bumped it to "4.B".
    This test stays valid across the 4.x line — the precise current
    version is asserted by the per-phase test (e.g.
    tests/test_phase_4b_precision_prompt.py::test_version_bumped_to_4b_in_both_prompts).
    """
    for prompt in (LLM_PROMPT, OPUS_PROMPT):
        text = prompt.read_text(encoding="utf-8")
        m = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
        assert m is not None, f"no version line in {prompt.name}"
        assert m.group(1).startswith("4."), (
            f"{prompt.name} version is {m.group(1)!r}, expected '4.x'"
        )
