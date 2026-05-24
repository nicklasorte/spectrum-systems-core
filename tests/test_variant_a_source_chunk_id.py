"""Phase 5 Variant A — tests for the source_chunk_id prompt instruction.

The change is prompt-only: both meeting_minutes_llm.md and
meeting_minutes_opus.md gain a `## source_chunk_id (strongly
recommended)` section inside the VARIANT_A_SOURCE_CHUNK_ID block.

These tests pin the presence of the instruction in both prompts and
assert the two copies are byte-identical inside the variant block, so
Haiku and Opus runs receive the same guidance and the comparison
remains apples-to-apples.

The grounding gate's behaviour when `source_chunk_id` is present is
already covered by tests/promotion/test_grounding_gate.py; we do not
duplicate that here.
"""
from __future__ import annotations

from pathlib import Path

import pytest


PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
)
HAIKU_PROMPT = PROMPTS_DIR / "meeting_minutes_llm.md"
OPUS_PROMPT = PROMPTS_DIR / "meeting_minutes_opus.md"

_BEGIN = "<!-- VARIANT_A_SOURCE_CHUNK_ID_BEGIN -->"
_END = "<!-- VARIANT_A_SOURCE_CHUNK_ID_END -->"


def _extract_block(prompt_path: Path) -> str:
    text = prompt_path.read_text(encoding="utf-8")
    if _BEGIN not in text or _END not in text:
        raise AssertionError(
            f"{prompt_path.name} is missing the VARIANT_A source_chunk_id block"
        )
    start = text.index(_BEGIN) + len(_BEGIN)
    end = text.index(_END)
    return text[start:end]


def test_source_chunk_id_instruction_in_both_prompts() -> None:
    """Both prompts MUST carry the source_chunk_id instruction.

    The grounding gate's chunk-scoped check only fires when the model
    actually emits source_chunk_id. Without this prompt instruction the
    baseline produced 54 gate warnings (fall-back to full-transcript
    check). A missing block in either prompt is a regression.
    """
    haiku_block = _extract_block(HAIKU_PROMPT)
    opus_block = _extract_block(OPUS_PROMPT)

    for block, name in [(haiku_block, "haiku"), (opus_block, "opus")]:
        assert "source_chunk_id" in block, (
            f"{name} prompt block missing 'source_chunk_id' field name"
        )
        assert "strongly recommended" in block, (
            f"{name} prompt block missing the 'strongly recommended' header"
        )
        assert "turn_id" in block, (
            f"{name} prompt block must reference turn_id "
            "(the existing chunk identifier in the TURN block)"
        )
        assert "TRANSCRIPT TURNS" in block, (
            f"{name} prompt block must point the model at the existing "
            "TRANSCRIPT TURNS lookup table"
        )
        assert "chunk-scoped" in block or "specific chunk" in block, (
            f"{name} prompt block must explain the chunk-scoped check"
        )


def test_source_chunk_id_instruction_byte_identical() -> None:
    """The two prompts' source_chunk_id blocks MUST be byte-identical.

    Haiku and Opus are compared head-to-head; any drift between their
    source_chunk_id guidance would confound the comparison.
    """
    haiku_block = _extract_block(HAIKU_PROMPT)
    opus_block = _extract_block(OPUS_PROMPT)
    assert haiku_block == opus_block, (
        "VARIANT_A source_chunk_id block has drifted between the Haiku "
        "and Opus prompts. The two copies must be byte-identical so the "
        "head-to-head comparison stays apples-to-apples."
    )


def test_source_chunk_id_block_appears_after_verbatim_grounding() -> None:
    """The instruction must follow the VERBATIM SOURCE GROUNDING section.

    source_chunk_id is a refinement of the source_quote contract — it
    only makes sense once the model has read the verbatim-grounding
    rules. Placing it before would invert the conceptual order.
    """
    for prompt_path in (HAIKU_PROMPT, OPUS_PROMPT):
        text = prompt_path.read_text(encoding="utf-8")
        verbatim_idx = text.index("## VERBATIM SOURCE GROUNDING")
        chunk_id_idx = text.index(_BEGIN)
        assert chunk_id_idx > verbatim_idx, (
            f"{prompt_path.name}: VARIANT_A block must follow VERBATIM "
            "SOURCE GROUNDING (it's a refinement, not a prerequisite)"
        )


def test_changelog_records_phase_5_a() -> None:
    """The frontmatter changelog MUST advertise the Variant A change.

    The prompt content_hash is captured in the artifact's
    extraction_config; a phase rename in the changelog forces an
    operator to re-baseline rather than silently swapping prompts.
    """
    for prompt_path in (HAIKU_PROMPT, OPUS_PROMPT):
        text = prompt_path.read_text(encoding="utf-8")
        assert "Phase 5.A" in text, (
            f"{prompt_path.name} changelog missing 'Phase 5.A' entry"
        )
        assert "source_chunk_id" in text.split("---")[1], (
            f"{prompt_path.name} frontmatter changelog must mention "
            "source_chunk_id so the Variant A change is discoverable from "
            "the prompt's own metadata"
        )


def test_source_chunk_id_block_shows_correct_example() -> None:
    """The block MUST show a usable JSON example so the model copies it.

    The model's most reliable signal is a literal example in the prompt.
    The example MUST use a turn_id token shape (t followed by digits)
    that matches the existing turn-block format.
    """
    import re

    for prompt_path in (HAIKU_PROMPT, OPUS_PROMPT):
        block = _extract_block(prompt_path)
        # Must show a JSON-shaped example of the field
        assert '"source_chunk_id"' in block, (
            f"{prompt_path.name}: VARIANT_A block missing a JSON-shaped "
            "example of the source_chunk_id field"
        )
        # Must show a turn_id token (e.g. t0042) as the example value
        assert re.search(r'"t\d+"', block), (
            f"{prompt_path.name}: VARIANT_A block must show a turn_id "
            "token like \"t0042\" as the example value"
        )
