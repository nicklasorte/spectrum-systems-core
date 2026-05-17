"""Confidence field: schema, threshold flagging, aligner integration.

Confidence is a required field on every extracted item. Items below
``CONFIDENCE_THRESHOLD`` (0.5) are flagged but kept -- they go to HITL
review, not the bit bucket. The EvalAligner surfaces the same flag in
its review_alignments output as ``low_confidence_flagged`` for later
calibration analysis.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from spectrum_systems_core.evals.m4.aligner import EvalAligner
from spectrum_systems_core.extraction._prompt_blocks import (
    CONFIDENCE_SCORING_BLOCK,
    CONFIDENCE_THRESHOLD,
)
from spectrum_systems_core.extraction.action_item_extractor import (
    ActionItemExtractor,
)
from spectrum_systems_core.extraction.claim_extractor import ClaimExtractor
from spectrum_systems_core.extraction.decision_extractor import (
    DecisionExtractor,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "extraction"
    / "meeting_extraction.schema.json"
)


def _capture_prompt() -> tuple[list[str], Callable[[str], dict[str, Any]]]:
    captured: list[str] = []

    def caller(prompt: str) -> dict[str, Any]:
        captured.append(prompt)
        return {"items": []}

    return captured, caller


def _chunk(cid: str = "c1") -> dict[str, Any]:
    return {"chunk_id": cid, "speaker": "S", "text": "Plan approved."}


# ---------------------------------------------------------------------------
# Schema-level: confidence is required on items in every type.

def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "field_path",
    [
        ("decisions",),
        ("claims",),
        ("action_items",),
    ],
)
def test_confidence_field_required_in_schema(field_path) -> None:
    schema = _load_schema()
    section = schema["properties"][field_path[0]]["items"]
    assert "confidence" in section["required"], (
        f"confidence must be a required property in {field_path[0]} items"
    )
    assert section["properties"]["confidence"] == {
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
    }


def test_schema_rejects_item_without_confidence() -> None:
    schema = _load_schema()
    bad_artifact = {
        "meeting_extraction_id": "00000000-0000-0000-0000-000000000000",
        "source_artifact_id": "00000000-0000-0000-0000-000000000000",
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "decisions": [{
            "decision_text": "x",
            "decision_type": "approved",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["t1"],
            "source_turn_validation": "verified",
            # No "confidence" -> must fail.
        }],
        "claims": [],
        "action_items": [],
        "total_chunks_classified": 0,
        "off_topic_count": 0,
        "regulatory_verb_fallback_count": 0,
        "routing_quality_warning": False,
        "requires_human_dedup_count": 0,
        "extraction_run_id": "r-1",
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": True,
        "confidence_threshold": 0.5,
        "low_confidence_item_count": 0,
        "provenance": {"produced_by": "ExtractionMerger"},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad_artifact)


# ---------------------------------------------------------------------------
# Threshold flagging: extractor produces flagged items below threshold.

def test_low_confidence_decision_flagged_for_review() -> None:
    def caller(prompt: str) -> dict[str, Any]:  # noqa: ARG001
        return {"items": [{
            "decision_text": "weak signal",
            "decision_type": "noted",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["c1"],
            "confidence": 0.3,
        }]}

    ex = DecisionExtractor(api_caller=caller)
    items = ex.extract([_chunk()], "", available_turn_ids={"c1"})
    assert len(items) == 1
    item = items[0]
    assert item["confidence"] == 0.3
    assert item["items_requiring_review"] is True
    assert item["review_reason"] == "low_confidence"
    assert ex.last_run_metadata["low_confidence_item_count"] == 1


def test_high_confidence_item_not_auto_flagged() -> None:
    def caller(prompt: str) -> dict[str, Any]:  # noqa: ARG001
        return {"items": [{
            "decision_text": "strong signal",
            "decision_type": "approved",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["c1"],
            "confidence": 0.9,
        }]}

    ex = DecisionExtractor(api_caller=caller)
    items = ex.extract([_chunk()], "", available_turn_ids={"c1"})
    item = items[0]
    # The extractor must not have set items_requiring_review for this reason.
    assert "items_requiring_review" not in item or item.get("items_requiring_review") is False
    assert ex.last_run_metadata["low_confidence_item_count"] == 0


def test_zero_confidence_item_flagged_not_dropped() -> None:
    """confidence=0.0 must NOT silently drop the item -- flag for HITL."""
    def caller(prompt: str) -> dict[str, Any]:  # noqa: ARG001
        return {"items": [{
            "decision_text": "ambiguous",
            "decision_type": "noted",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["c1"],
            "confidence": 0.0,
        }]}

    ex = DecisionExtractor(api_caller=caller)
    items = ex.extract([_chunk()], "", available_turn_ids={"c1"})
    assert len(items) == 1, "confidence=0.0 items go to review, not the bit bucket"
    assert items[0]["items_requiring_review"] is True
    assert items[0]["review_reason"] == "low_confidence"


def test_missing_confidence_defaults_to_zero_and_flags() -> None:
    """If the model omits confidence, treat as 0.0 -> flag for review."""
    def caller(prompt: str) -> dict[str, Any]:  # noqa: ARG001
        return {"items": [{
            "decision_text": "model forgot to score",
            "decision_type": "noted",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["c1"],
        }]}

    ex = DecisionExtractor(api_caller=caller)
    items = ex.extract([_chunk()], "", available_turn_ids={"c1"})
    assert items[0]["confidence"] == 0.0
    assert items[0]["items_requiring_review"] is True


# ---------------------------------------------------------------------------
# Run-level counts on the merged artifact.

def test_low_confidence_count_recorded_in_meeting_extraction() -> None:
    from spectrum_systems_core.extraction.extraction_merger import (
        ExtractionMerger,
    )

    def caller_mixed(prompt: str) -> dict[str, Any]:  # noqa: ARG001
        return {"items": [
            {
                "decision_text": "above threshold",
                "decision_type": "approved",
                "stakeholders": [],
                "rationale": None,
                "source_turn_ids": ["c1"],
                "confidence": 0.9,
            },
            {
                "decision_text": "below threshold A",
                "decision_type": "noted",
                "stakeholders": [],
                "rationale": None,
                "source_turn_ids": ["c1"],
                "confidence": 0.3,
            },
            {
                "decision_text": "below threshold B",
                "decision_type": "noted",
                "stakeholders": [],
                "rationale": None,
                "source_turn_ids": ["c1"],
                "confidence": 0.1,
            },
        ]}

    ex = DecisionExtractor(api_caller=caller_mixed)
    decisions = ex.extract([_chunk()], "", available_turn_ids={"c1"})

    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        extraction_run_id="run-1",
        classifications=[],
        decisions=decisions,
        claims=[],
        action_items=[],
        run_metadata=[ex.last_run_metadata],
    )
    assert artifact["confidence_threshold"] == CONFIDENCE_THRESHOLD
    assert artifact["low_confidence_item_count"] == 2


# ---------------------------------------------------------------------------
# Prompt contents: confidence scoring block appears.

@pytest.mark.parametrize(
    "extractor_cls",
    [DecisionExtractor, ClaimExtractor, ActionItemExtractor],
)
def test_confidence_scoring_block_in_extractor_prompt(extractor_cls) -> None:
    captured, caller = _capture_prompt()
    extractor_cls(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    assert "CONFIDENCE SCORING:" in prompt
    assert "If you would score an item below 0.5: OMIT" in prompt
    assert CONFIDENCE_SCORING_BLOCK in prompt


@pytest.mark.parametrize(
    "extractor_cls",
    [DecisionExtractor, ClaimExtractor, ActionItemExtractor],
)
def test_confidence_block_position_after_output_schema(extractor_cls) -> None:
    captured, caller = _capture_prompt()
    extractor_cls(api_caller=caller).extract(
        [_chunk()], "", available_turn_ids={"c1"}
    )
    prompt = captured[0]
    schema_idx = prompt.find("OUTPUT SCHEMA")
    conf_idx = prompt.find("CONFIDENCE SCORING")
    assert schema_idx >= 0 and conf_idx >= 0
    assert schema_idx < conf_idx, (
        "Confidence scoring instructions must follow the output schema "
        "section (model sees the schema, then how to score each field)."
    )


# ---------------------------------------------------------------------------
# EvalAligner: low-confidence flag flows through review_alignments.

def test_eval_aligner_tags_low_confidence_review_entry() -> None:
    aligner = EvalAligner()
    extracted = [{
        "text": "Approve plan A",
        "items_requiring_review": True,
        "review_reason": "low_confidence",
    }]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text="DECISION: Approve plan A",
        source_id="src-1",
        minutes_artifact_id="min-1",
    )
    assert len(result["review_alignments"]) == 1
    entry = result["review_alignments"][0]
    assert entry["low_confidence_flagged"] is True


def test_eval_aligner_no_flag_for_high_confidence_item() -> None:
    aligner = EvalAligner()
    extracted = [{
        "text": "Approve plan A",
        # No low-confidence flag.
    }]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text="DECISION: Approve plan A",
        source_id="src-1",
        minutes_artifact_id="min-1",
    )
    entry = result["review_alignments"][0]
    assert entry["low_confidence_flagged"] is False


def test_eval_aligner_flag_carries_through_meeting_extraction_path() -> None:
    """``items_from_meeting_extraction`` -> aligner -> review_alignments tag."""
    aligner = EvalAligner()
    meeting_extraction = {
        "decisions": [{
            "decision_text": "Approve plan A",
            "decision_type": "approved",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["t1"],
            "source_turn_validation": "verified",
            "confidence": 0.3,
            "items_requiring_review": True,
            "review_reason": "low_confidence",
        }],
        "claims": [],
        "action_items": [],
    }
    result = aligner.align_from_meeting_extraction(
        meeting_extraction=meeting_extraction,
        minutes_text="DECISION: Approve plan A",
        source_id="src-1",
        minutes_artifact_id="min-1",
    )
    assert any(
        entry.get("low_confidence_flagged") is True
        for entry in result["review_alignments"]
    )
