"""Phase 1 — orchestrator integration helper (`grounding_gated_payload`).

Red Team Pass 1 #1: silent-pass paths. The gate is the contract, but
the contract is only enforced if callers wire it into the promotion
pipeline. This module's tests prove the helper produces:

- a filtered payload with only accepted items per item-type array;
- an absent item-type stays absent (we don't add empty arrays the
  artifact never emitted);
- pass-through for every payload field outside the item-type table
  (provenance, title, summary, schema_version, etc.);
- the GroundingReport object the orchestrator needs to decide
  block vs. allow.
"""
from __future__ import annotations

from spectrum_systems_core.artifacts.model import new_artifact
from spectrum_systems_core.promotion.promoter import grounding_gated_payload


def test_helper_filters_rejected_items_only(tmp_path):
    transcript = "CHAIR: Hello world. CHAIR: Goodbye now."
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "summary text",
        "decisions": [
            # Real, grounded.
            {
                "text": "Hello world.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello world.",
                "quote_offset_normalized": 6,
            },
            # Fabricated.
            {
                "text": "We approved the plan.",
                "grounding_mode": "verbatim",
                "source_quote": "We approved the plan.",
                "quote_offset_normalized": 0,
            },
        ],
        "action_items": [],
        "open_questions": [],
        "provenance": {"produced_by": "meeting_minutes_llm"},
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    filtered, report = grounding_gated_payload(art, transcript)
    # Only the grounded decision survived.
    assert len(filtered["decisions"]) == 1
    assert filtered["decisions"][0]["text"] == "Hello world."
    # Envelope-level pass-through fields preserved.
    assert filtered["title"] == "test"
    assert filtered["summary"] == "summary text"
    assert filtered["schema_version"] == "1.4.0"
    assert filtered["provenance"] == {"produced_by": "meeting_minutes_llm"}
    # The report mirrors what verify_grounding would have returned.
    assert len(report.rejected_items) == 1
    assert len(report.accepted_items) == 1


def test_helper_keeps_absent_item_types_absent():
    transcript = "CHAIR: Hello."
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "",
        "decisions": [
            {
                "text": "Hello.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello.",
                "quote_offset_normalized": 6,
            }
        ],
        "action_items": [],
        "open_questions": [],
        # claims / risks / etc. are absent — must stay absent.
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    filtered, report = grounding_gated_payload(art, transcript)
    # Item types we never emitted must not be added by the gate.
    assert "claims" not in filtered
    assert "risks" not in filtered
    assert "attendees" not in filtered
    assert report.artifact_blocked is False


def test_helper_replaces_all_rejected_type_with_empty_list():
    """If every item of a type was rejected, the helper replaces the
    array with []. The rejected items must NOT survive in the
    filtered payload."""
    transcript = "CHAIR: Hello world."
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "",
        "decisions": [
            {
                "text": "Fake decision.",
                "grounding_mode": "verbatim",
                "source_quote": "Fake decision.",
                "quote_offset_normalized": 0,
            }
        ],
        "action_items": [],
        "open_questions": [],
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    filtered, _ = grounding_gated_payload(art, transcript)
    assert filtered["decisions"] == []


def test_helper_signals_block_when_grounding_rate_below_floor():
    transcript = "CHAIR: Real quote here."
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "",
        "decisions": [
            {
                "text": f"D{i}",
                "grounding_mode": "verbatim",
                "source_quote": f"Fake quote {i}.",
                "quote_offset_normalized": 0,
            }
            for i in range(5)
        ],
        "action_items": [],
        "open_questions": [],
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    _, report = grounding_gated_payload(art, transcript)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "grounding_rate_below_floor"


def test_helper_signals_block_when_transcript_unreadable():
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    _, report = grounding_gated_payload(art, None)
    assert report.artifact_blocked is True
    assert report.block_reason_code == "transcript_unreadable"


def test_helper_does_not_mutate_input_payload():
    """Defensive: the helper must not edit the artifact's payload in
    place — the constitution forbids mutating payload. The returned
    filtered payload is a NEW dict."""
    transcript = "CHAIR: Hello world."
    payload = {
        "schema_version": "1.4.0",
        "title": "test",
        "summary": "",
        "decisions": [
            {
                "text": "Hello world.",
                "grounding_mode": "verbatim",
                "source_quote": "Hello world.",
                "quote_offset_normalized": 6,
            },
            {
                "text": "Fake.",
                "grounding_mode": "verbatim",
                "source_quote": "Fake.",
                "quote_offset_normalized": 0,
            },
        ],
        "action_items": [],
        "open_questions": [],
    }
    art = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="t1",
    )
    original_len = len(art.payload["decisions"])
    grounding_gated_payload(art, transcript)
    # Original payload is unchanged.
    assert len(art.payload["decisions"]) == original_len
