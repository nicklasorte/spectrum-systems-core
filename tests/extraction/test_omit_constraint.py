"""OMIT constraint block: presence, position, propagation.

Per the Wan et al. positional-bias finding, the OMIT block must appear
BEFORE the chunk content in every typed-extractor prompt. Tests assert
that with prompt.index() rather than mere substring presence -- a wrong
position would otherwise pass a naive substring check.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

import pytest

from spectrum_systems_core.extraction._prompt_blocks import (
    OMIT_INSTRUCTION_BLOCK,
)
from spectrum_systems_core.extraction.action_item_extractor import (
    ActionItemExtractor,
)
from spectrum_systems_core.extraction.claim_extractor import ClaimExtractor
from spectrum_systems_core.extraction.decision_extractor import (
    DecisionExtractor,
)


_OMIT_MARKER = "CRITICAL CONSTRAINT"
_OMIT_DO_NOT_INFER = "Do not infer"
_OMIT_TAG = "OMIT IF NOT IN TRANSCRIPT"


def _capture_prompt() -> tuple[List[str], Callable[[str], Dict[str, Any]]]:
    captured: List[str] = []

    def caller(prompt: str) -> Dict[str, Any]:
        captured.append(prompt)
        return {"items": []}

    return captured, caller


_DISTINCT_CHUNK_TEXT = "ZZZ_DISTINCT_SENTINEL_CHUNK_TEXT_QQQ"


def _chunk() -> Dict[str, Any]:
    return {"chunk_id": "c1", "speaker": "S", "text": _DISTINCT_CHUNK_TEXT}


# ---------------------------------------------------------------------------
# Presence in every extractor prompt.

def test_omit_block_present_in_decision_extractor_prompt() -> None:
    captured, caller = _capture_prompt()
    DecisionExtractor(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    assert _OMIT_MARKER in prompt
    assert _OMIT_TAG in prompt
    assert _OMIT_DO_NOT_INFER in prompt
    assert "Uncertainty is not a reason to include" in prompt


def test_omit_block_present_in_claim_extractor_prompt() -> None:
    captured, caller = _capture_prompt()
    ClaimExtractor(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    assert _OMIT_MARKER in prompt
    assert _OMIT_TAG in prompt
    assert _OMIT_DO_NOT_INFER in prompt


def test_omit_block_present_in_action_item_extractor_prompt() -> None:
    captured, caller = _capture_prompt()
    ActionItemExtractor(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    assert _OMIT_MARKER in prompt
    assert _OMIT_TAG in prompt
    assert _OMIT_DO_NOT_INFER in prompt


# ---------------------------------------------------------------------------
# Position: OMIT block must appear BEFORE the chunk content.

@pytest.mark.parametrize(
    "extractor_cls",
    [DecisionExtractor, ClaimExtractor, ActionItemExtractor],
)
def test_omit_block_appears_before_chunk_content(extractor_cls) -> None:
    captured, caller = _capture_prompt()
    extractor_cls(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    omit_idx = prompt.find(_OMIT_MARKER)
    chunk_idx = prompt.find(_DISTINCT_CHUNK_TEXT)
    assert omit_idx >= 0, "OMIT block missing"
    assert chunk_idx >= 0, "chunk text missing"
    assert omit_idx < chunk_idx, (
        f"OMIT block must precede chunk content (positional bias); "
        f"got omit at {omit_idx}, chunk at {chunk_idx}"
    )


# ---------------------------------------------------------------------------
# Block byte-identical across the three extractors.

def test_omit_block_byte_identical_across_extractors() -> None:
    """All three extractors must emit the same OMIT block verbatim."""
    captures = {}
    for name, cls in (
        ("decision", DecisionExtractor),
        ("claim", ClaimExtractor),
        ("action_item", ActionItemExtractor),
    ):
        captured, caller = _capture_prompt()
        cls(api_caller=caller).extract([_chunk()], "", available_turn_ids={"c1"})
        captures[name] = captured[0]
    for name, prompt in captures.items():
        assert OMIT_INSTRUCTION_BLOCK in prompt, (
            f"{name} extractor's prompt does not contain the canonical OMIT block"
        )


# ---------------------------------------------------------------------------
# Propagation: extractor metadata records the instruction was present.

@pytest.mark.parametrize(
    "extractor_cls",
    [DecisionExtractor, ClaimExtractor, ActionItemExtractor],
)
def test_omit_instruction_present_recorded_in_metadata(extractor_cls) -> None:
    _, caller = _capture_prompt()
    ex = extractor_cls(api_caller=caller)
    ex.extract([_chunk()], "", available_turn_ids={"c1"})
    assert ex.last_run_metadata["omit_instruction_present"] is True


def test_omit_metadata_starts_false_before_any_extraction() -> None:
    """Fresh extractor: omit_instruction_present is False until extract() renders a prompt.

    Guards against the field being a decorative ``True`` constant that
    drifts from prompt reality.
    """
    ex = DecisionExtractor()
    assert ex.last_run_metadata["omit_instruction_present"] is False


def test_omit_metadata_stays_false_when_extract_short_circuits() -> None:
    """Empty chunks -> no prompt is built -> omit_instruction_present stays False."""
    ex = DecisionExtractor()
    items = ex.extract([], "", available_turn_ids=set())
    assert items == []
    assert ex.last_run_metadata["omit_instruction_present"] is False


def test_meeting_extraction_artifact_records_omit_instruction_present() -> None:
    """The merged artifact reports omit_instruction_present=True."""
    from spectrum_systems_core.extraction.extraction_merger import (
        ExtractionMerger,
    )

    _, caller = _capture_prompt()
    ex = DecisionExtractor(api_caller=caller)
    ex.extract([_chunk()], "", available_turn_ids={"c1"})

    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        extraction_run_id="run-1",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
        run_metadata=[ex.last_run_metadata],
    )
    assert artifact["omit_instruction_present"] is True
