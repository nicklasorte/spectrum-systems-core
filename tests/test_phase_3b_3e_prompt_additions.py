"""Phase 3.B-E — prompt-additions contract tests.

These tests pin the four prompt-engineering techniques added in this
PR so a future edit cannot quietly drop or drift them out of lockstep
between the Haiku production prompt (``meeting_minutes_llm.md``) and
the Opus reference prompt (``meeting_minutes_opus.md``).

Contracts asserted here:

- 3.B: ``IMPLICIT DECISION RECOGNITION`` section present in both prompts
  with at least 5 of the canonical linguistic markers.
- 3.C: ``MODAL VERB POLICY`` section present in both prompts with each
  of shall/will/should/may carrying a classification rule.
- 3.D: NTIA/DoD spectrum glossary present in both prompts with at least
  30 of the canonical terms.
- 3.E: exactly three few-shot examples present, in the correct order
  (explicit → near-miss → implicit), with the section byte-identical
  between the two prompts.
- No regression: the Phase 3.A ``DO NOT EXTRACT`` section is still
  present and unchanged.
- The prompt versions are higher than the Phase 3.A baseline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
)
_HAIKU_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_llm.md"
_OPUS_PROMPT_PATH = _PROMPTS_DIR / "meeting_minutes_opus.md"


def _extract_block(text: str, begin_marker: str, end_marker: str) -> str:
    """Return the text strictly between ``begin_marker`` and
    ``end_marker``. Both markers MUST appear or pytest fails with a
    clear message naming which marker was missing."""
    start = text.find(begin_marker)
    if start == -1:
        pytest.fail(f"begin marker not found: {begin_marker!r}")
    after_begin = start + len(begin_marker)
    end = text.find(end_marker, after_begin)
    if end == -1:
        pytest.fail(
            f"end marker {end_marker!r} not found after begin marker "
            f"{begin_marker!r}"
        )
    return text[after_begin:end]


def _strip_trailing_whitespace(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines())


# ---------------------------------------------------------------------------
# Test 1: IMPLICIT DECISION RECOGNITION section present in both prompts
# ---------------------------------------------------------------------------


def test_taxonomy_section_present_in_both_prompts() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        assert "## IMPLICIT DECISION RECOGNITION" in text, (
            f"{label} prompt missing the `## IMPLICIT DECISION "
            f"RECOGNITION` section (Phase 3.B)"
        )
        assert "Fernández" in text, (
            f"{label} prompt missing the Fernández attribution in "
            "the IMPLICIT DECISION RECOGNITION section"
        )
        # Each of the four sub-types must be present.
        for subtype_header in (
            "### Issue identification",
            "### Proposal / Direction",
            "### Resolution",
            "### Scope / Boundary ruling",
        ):
            assert subtype_header in text, (
                f"{label} prompt IMPLICIT DECISION RECOGNITION section "
                f"missing sub-type header {subtype_header!r}"
            )
        # decision_subtype enum values must appear in the section.
        for enum_value in ('"issue"', '"proposal"', '"resolution"', '"scope"'):
            assert enum_value in text, (
                f"{label} prompt missing decision_subtype enum value "
                f"{enum_value} in the taxonomy section"
            )


# ---------------------------------------------------------------------------
# Test 2: taxonomy linguistic markers present (at least 5 of the canonical ones)
# ---------------------------------------------------------------------------


_REQUIRED_MARKERS: tuple[str, ...] = (
    "let's go with",
    "the path forward is",
    "we'll [verb] [object]",
    "we're going to",
    "sounds good, we'll",
)


def test_taxonomy_markers_present() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        hits = [m for m in _REQUIRED_MARKERS if m in text]
        assert len(hits) >= 5, (
            f"{label} prompt has only {len(hits)} of the required "
            f"taxonomy markers (need >= 5). Hits: {hits!r}. "
            f"Required: {_REQUIRED_MARKERS!r}"
        )

    # Anti-regression: "what if we" must NOT appear as a proposal marker
    # because it is explicitly excluded by the DO NOT EXTRACT section
    # (hedging). The phrase MAY still appear inside the DO NOT EXTRACT
    # section itself — we scope the check to the IMPLICIT DECISION
    # RECOGNITION block only.
    for label, text in (("haiku", haiku), ("opus", opus)):
        block = _extract_block(
            text,
            "<!-- TAXONOMY_3B_BEGIN -->",
            "<!-- TAXONOMY_3B_END -->",
        )
        assert "what if we" not in block, (
            f"{label} prompt IMPLICIT DECISION RECOGNITION section "
            "includes the marker 'what if we' — that contradicts the "
            "DO NOT EXTRACT brainstorming category and must be removed"
        )


# ---------------------------------------------------------------------------
# Test 3: MODAL VERB POLICY section present in both prompts
# ---------------------------------------------------------------------------


def test_modal_policy_section_present_in_both_prompts() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        assert "## MODAL VERB POLICY" in text, (
            f"{label} prompt missing the `## MODAL VERB POLICY` "
            "section (Phase 3.C)"
        )
        block = _extract_block(
            text,
            "<!-- MODAL_3C_BEGIN -->",
            "<!-- MODAL_3C_END -->",
        )
        for modal in ('**"shall"**', '**"will"**', '**"should"**', '**"may"**'):
            assert modal in block, (
                f"{label} prompt MODAL VERB POLICY missing classification "
                f"rule for modal {modal}"
            )


# ---------------------------------------------------------------------------
# Test 4: glossary present with at least 30 terms
# ---------------------------------------------------------------------------


# Canonical sample drawn from the Phase 3.D glossary spec. The test
# requires at least 30 of the 36 listed below to appear in EACH prompt's
# glossary block. Each entry is the bolded heading the glossary uses.
_GLOSSARY_TERMS: tuple[str, ...] = (
    "**Allocation**",
    "**Assignment**",
    "**CBRS (Citizens Broadband Radio Service)**",
    "**COA (Course of Action)**",
    "**CUI (Controlled Unclassified Information)**",
    "**DFS (Dynamic Frequency Selection)**",
    "**DoD (Department of Defense)**",
    "**Downlink**",
    "**ERP (Effective Radiated Power)**",
    "**FAS (Frequency Assignment Subcommittee)**",
    "**Fixed Service (FS)**",
    "**Fixed-Satellite Service (FSS)**",
    "**FSS receiver**",
    "**GMF (Government Master File)**",
    "**IRAC (Interdepartment Radio Advisory Committee)**",
    "**ITU (International Telecommunication Union)**",
    "**LTE (Long-Term Evolution)**",
    "**Metsat (Meteorological Satellite)**",
    "**MHz (Megahertz)**",
    "**Mobile Service (MS)**",
    "**Mobile-Satellite Service (MSS)**",
    "**NR (New Radio)**",
    "**OB3**",
    "**Point-to-Point Microwave**",
    "**Primary allocation**",
    "**Protection zone**",
    "**Radiolocation Service**",
    "**Radionavigation Service**",
    "**Secondary allocation**",
    "**Space Force**",
    "**Spectrum sharing**",
    "**Study plan**",
    "**System list**",
    "**TIG (Technical Implementation Group)**",
    "**Uplink**",
    "**WGS (Wideband Global SATCOM)**",
    "**Working group**",
)


def test_glossary_present_with_minimum_terms() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        assert "## NTIA/DoD SPECTRUM GLOSSARY" in text, (
            f"{label} prompt missing the `## NTIA/DoD SPECTRUM "
            "GLOSSARY` section (Phase 3.D)"
        )
        block = _extract_block(
            text,
            "<!-- GLOSSARY_3D_BEGIN -->",
            "<!-- GLOSSARY_3D_END -->",
        )
        hits = [term for term in _GLOSSARY_TERMS if term in block]
        assert len(hits) >= 30, (
            f"{label} prompt glossary contains only {len(hits)} of the "
            f"canonical terms (need >= 30). Hits: {hits!r}"
        )

    # Glossary block must be byte-identical between the two prompts so
    # the Haiku/Opus F1 comparison is anchored on the same definitions.
    haiku_block = _extract_block(
        haiku, "<!-- GLOSSARY_3D_BEGIN -->", "<!-- GLOSSARY_3D_END -->"
    )
    opus_block = _extract_block(
        opus, "<!-- GLOSSARY_3D_BEGIN -->", "<!-- GLOSSARY_3D_END -->"
    )
    assert _strip_trailing_whitespace(haiku_block) == _strip_trailing_whitespace(
        opus_block
    ), (
        "Glossary block drifted between meeting_minutes_llm.md and "
        "meeting_minutes_opus.md — they must stay byte-identical so the "
        "F1 comparison stays interpretable."
    )


# ---------------------------------------------------------------------------
# Test 5: exactly three few-shot examples in the correct order
# ---------------------------------------------------------------------------


def test_three_fewshot_examples_present() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        block = _extract_block(
            text,
            "<!-- FEW_SHOT_3E_BEGIN -->",
            "<!-- FEW_SHOT_3E_END -->",
        )
        # Each example carries its own `### Example N:` header.
        example_headers = re.findall(r"^### Example (\d+):", block, re.MULTILINE)
        assert example_headers == ["1", "2", "3"], (
            f"{label} prompt few-shot section has example headers "
            f"{example_headers!r}; expected exactly [1, 2, 3]"
        )
        # Order: explicit → near-miss → implicit (most-similar last
        # per recency bias). Anchored on canonical title fragments.
        idx_explicit = block.find("Example 1: Explicit decision")
        idx_near_miss = block.find("Example 2: Near-miss non-decision")
        idx_implicit = block.find("Example 3: Implicit guidance-as-decision")
        assert 0 <= idx_explicit < idx_near_miss < idx_implicit, (
            f"{label} prompt few-shot examples not in canonical order "
            f"(explicit={idx_explicit}, near-miss={idx_near_miss}, "
            f"implicit={idx_implicit}); expected explicit < near-miss "
            "< implicit"
        )


# ---------------------------------------------------------------------------
# Test 6: few-shot section byte-identical between the two prompts
# ---------------------------------------------------------------------------


def test_fewshot_examples_byte_identical() -> None:
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    haiku_block = _extract_block(
        haiku, "<!-- FEW_SHOT_3E_BEGIN -->", "<!-- FEW_SHOT_3E_END -->"
    )
    opus_block = _extract_block(
        opus, "<!-- FEW_SHOT_3E_BEGIN -->", "<!-- FEW_SHOT_3E_END -->"
    )

    assert _strip_trailing_whitespace(haiku_block) == _strip_trailing_whitespace(
        opus_block
    ), (
        "Few-shot examples section drifted between meeting_minutes_llm.md "
        "and meeting_minutes_opus.md — they must stay byte-identical so "
        "the Haiku/Opus F1 comparison is anchored on the same demonstrated "
        "extraction shapes."
    )


# ---------------------------------------------------------------------------
# Test 7: DO NOT EXTRACT section from Phase 3.A is unchanged
# ---------------------------------------------------------------------------


def test_negative_section_still_present() -> None:
    """Anti-regression for Phase 3.A. The DO NOT EXTRACT section must
    still be present in both prompts and the eight category headers
    must still be there."""
    haiku = _HAIKU_PROMPT_PATH.read_text(encoding="utf-8")
    opus = _OPUS_PROMPT_PATH.read_text(encoding="utf-8")

    for label, text in (("haiku", haiku), ("opus", opus)):
        assert "## DO NOT EXTRACT" in text, (
            f"{label} prompt missing the Phase 3.A `## DO NOT EXTRACT` "
            "section — this is a regression"
        )
        for category in (
            "### Brainstorming and hypothetical reasoning",
            "### Recapping prior decisions",
            "### Restating the agenda",
            "### Conditional or speculative statements",
            "### Meta-procedural talk about the meeting itself",
            "### Quotes attributed to absent third parties",
            "### Repeated mentions of the same item",
            "### Banding / numeric mentions without context",
        ):
            assert category in text, (
                f"{label} prompt DO NOT EXTRACT section missing the "
                f"Phase 3.A subsection {category!r} — this is a regression"
            )


# ---------------------------------------------------------------------------
# Test 8: prompt versions incremented past 3.A
# ---------------------------------------------------------------------------


_YAML_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def test_prompt_versions_incremented() -> None:
    for path in (_HAIKU_PROMPT_PATH, _OPUS_PROMPT_PATH):
        text = path.read_text(encoding="utf-8")
        match = _YAML_VERSION_RE.search(text)
        assert match, f"{path.name} missing YAML `version:` frontmatter key"
        version = match.group(1)
        assert version != "3.A", (
            f"{path.name} version is still `3.A` — Phase 3.B-E must "
            "bump the version"
        )
        # The bumped version must lexically reflect 3.B or later. We
        # accept any string that sorts after `3.A` (e.g. `3.B`, `3.B-E`,
        # `3.E`, `4.0`) so a future cleanup re-ordering does not break
        # this test.
        assert version > "3.A", (
            f"{path.name} version {version!r} does not sort after the "
            "Phase 3.A baseline `3.A`"
        )
        # Changelog must reference the new phases.
        for phase in ("Phase 3.B", "Phase 3.C", "Phase 3.D", "Phase 3.E"):
            assert phase in text, (
                f"{path.name} YAML frontmatter changelog missing a "
                f"{phase} entry"
            )
