"""Schema-compliance contract for the Opus reference-baseline prompt.

The Opus prompt at ``workflows/prompts/meeting_minutes_opus.md`` drives
the ``opus`` and ``sonnet-unconstrained`` CLI variants. Both variants
go through the governed loop and are gated by
``llm_extraction_strict_schema``, which validates the assembled
artifact against ``schemas/meeting_minutes.schema.json``.

Before Phase 4a's schema-1.4.0 grounding-mode discriminators, the Opus
prompt instructed the model to place ``source_turn_ids`` on every
emitted item (including verbatim types like ``decisions``,
``claims``, ``risks``). The schema declares
``additionalProperties: false`` on those items and does NOT permit
``source_turn_ids`` on them — so an artifact that followed the old
prompt literally would fail the strict-schema gate. The same gate
also expects ``grounding_mode`` / ``quote_offset_original`` /
``reason`` on the relevant verbatim items.

This contract test pins the two halves of the fix:

1.  The prompt MUST instruct the verbatim/turn-aggregate split
    correctly — i.e. mention ``grounding_mode``, both mode tokens,
    ``source_quote``, ``quote_offset_normalized``,
    ``quote_offset_original``, ``source_turn_ids``, and the
    ``reason`` field.

2.  A hand-crafted minimal response that follows the prompt literally
    — one item in every one of the 22 content arrays, with each
    item carrying the grounding fields the prompt now prescribes —
    MUST validate against ``meeting_minutes.schema.json`` and pass
    the strict-schema eval.

Both halves must hold together: prompt directives without a
schema-valid example would let drift creep back in, and a
schema-valid example without prompt directives would not reach the
model at all.
"""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts.model import new_artifact
from spectrum_systems_core.evals.llm_extraction import (
    STRICT_SCHEMA_EVAL_TYPE,
    run_llm_strict_schema_eval,
)
from spectrum_systems_core.validation import validate_artifact
from spectrum_systems_core.workflows.model_selection import OPUS_PROMPT_PATH


# The 22 content arrays the governed pipeline expects (plus the
# ``grounding`` companion array, totaling 23). Mirrors
# ``tlc_router._ALL_CONTENT_ARRAYS`` so the prompt's directive and the
# router's accepted set cannot drift silently.
_CONTENT_ARRAYS: tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
    "commitments",
    "risks",
    "claims",
    "cross_references",
    "attendees",
    "topics",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
    "sentiment_indicators",
    "meeting_phases",
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)


def _opus_prompt_text() -> str:
    """Read the on-disk Opus prompt the CLI dispatches to."""
    assert OPUS_PROMPT_PATH.exists(), (
        f"OPUS_PROMPT_PATH does not exist: {OPUS_PROMPT_PATH}"
    )
    return OPUS_PROMPT_PATH.read_text(encoding="utf-8")


# -------- prompt-content directives -------------------------------------


def test_opus_prompt_declares_grounding_mode_discriminator() -> None:
    """Phase 4a grounding-mode discriminator must be in the prompt.

    Without ``grounding_mode`` in the instruction, the model cannot
    emit the schema's discriminator and the gate cannot route the
    item to the right verifier.
    """
    text = _opus_prompt_text()
    assert "grounding_mode" in text, (
        "Opus prompt must instruct the model to emit `grounding_mode` on "
        "every structured item (schema_version 1.4.0 discriminator)."
    )
    assert '"verbatim"' in text, (
        "Opus prompt must declare the `verbatim` grounding-mode token "
        "(the value the verbatim-type sub-schemas pin to via `const`)."
    )
    assert '"turn_aggregate"' in text, (
        "Opus prompt must declare the `turn_aggregate` grounding-mode "
        "token (the value the turn-aggregate-type sub-schemas pin to)."
    )


def test_opus_prompt_lists_verbatim_grounding_fields() -> None:
    """Verbatim-mode items carry source_quote + both byte offsets."""
    text = _opus_prompt_text()
    for field in (
        "source_quote",
        "quote_offset_normalized",
        "quote_offset_original",
    ):
        assert field in text, (
            f"Opus prompt must instruct the model to emit `{field}` on "
            "verbatim items — the strict-schema gate rejects a 1.4.0 "
            "verbatim item missing this field."
        )


def test_opus_prompt_lists_turn_aggregate_grounding_fields() -> None:
    """Turn-aggregate items carry source_turn_ids (and only that)."""
    text = _opus_prompt_text()
    assert "source_turn_ids" in text, (
        "Opus prompt must instruct the model to emit `source_turn_ids` "
        "on turn-aggregate items — the gate enforces the turn-id list "
        "for 1.4.0 turn_aggregate items."
    )


def test_opus_prompt_requires_reason_field() -> None:
    """Phase 3P `reason` field must be required by the prompt."""
    text = _opus_prompt_text()
    assert "reason" in text, (
        "Opus prompt must instruct the model to emit a `reason` field "
        "on decisions / action_items (Phase 3P, additive)."
    )


def test_opus_prompt_enumerates_every_content_array() -> None:
    """All 22 content arrays must be named in the output-schema block.

    The schema does not REQUIRE every array (only decisions /
    action_items / open_questions are required at the top level), but
    the governed loop's downstream consumers expect the prompt to
    instruct an exhaustive emission. A missing array name is a
    prompt-side drift that re-introduces silent under-extraction.
    """
    text = _opus_prompt_text()
    missing = [k for k in _CONTENT_ARRAYS if f'"{k}"' not in text]
    assert not missing, (
        "Opus prompt omitted these content arrays from its output "
        f"schema block: {missing}"
    )


# -------- schema-valid synthetic response -------------------------------


def _verbatim_grounding(quote: str = "we will proceed") -> dict[str, object]:
    """The fields every verbatim-mode item must carry.

    Returns the field set the schema sub-schemas accept for any
    VERBATIM item type. ``quote_offset_*`` use small placeholder
    integers — the strict-schema eval (which this test exercises)
    only checks shape; the byte-match itself is the
    ``promotion/gate.py`` layer.
    """
    return {
        "grounding_mode": "verbatim",
        "source_quote": quote,
        "quote_offset_normalized": 0,
        "quote_offset_original": 0,
    }


def _turn_aggregate_grounding() -> dict[str, object]:
    """The fields every turn-aggregate-mode item must carry."""
    return {
        "grounding_mode": "turn_aggregate",
        "source_turn_ids": [7],
    }


def _build_synthetic_response() -> dict[str, object]:
    """Build a hand-crafted response that follows the new prompt.

    Each of the 22 content arrays carries exactly one item. Verbatim
    items carry ``source_quote`` + both byte offsets +
    ``grounding_mode: "verbatim"``. Turn-aggregate items carry
    ``source_turn_ids`` + ``grounding_mode: "turn_aggregate"``.
    Decision / action-item items also carry the Phase 3P ``reason``.
    """
    quote = "we will proceed with the 7 GHz downlink threshold"
    return {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "title": "7 GHz Downlink TIG — synthetic test transcript",
        "summary": "Synthetic transcript used to pin Opus prompt compliance.",
        "decisions": [
            {
                "text": quote,
                "verb": "directed",
                "stakeholders": ["NTIA"],
                "confidence": 0.9,
                "rationale": "because the PCC directed it",
                "reason": "Explicit decision: 'we will proceed' with group affirmation.",
                **_verbatim_grounding(quote),
            }
        ],
        "action_items": [
            {
                "action": quote,
                "status": "open",
                "owner": "NTIA Lead",
                "due": "before the next session",
                "follow_up_required": True,
                "reason": "Procedural commitment: 'we will proceed' names a group act.",
                **_verbatim_grounding(quote),
            }
        ],
        "open_questions": [
            {
                "question_id": "q-1",
                "question_text": "How should we set the threshold?",
                "asked_by": "DoD Rep",
                "resolved": False,
                **_turn_aggregate_grounding(),
            }
        ],
        "commitments": [
            {
                "commitment_id": "c-1",
                "owner": "DoD Rep",
                "commitment_text": quote,
                "due": "before the next session",
                "source_speaker": "DoD Rep",
                **_verbatim_grounding(quote),
            }
        ],
        "risks": [
            {
                "risk_id": "r-1",
                "risk_text": quote,
                "raised_by": "DoD Rep",
                "severity": "medium",
                "mitigation_mentioned": None,
                **_verbatim_grounding(quote),
            }
        ],
        "claims": [
            {
                "claim_id": "cl-1",
                "claim_text": quote,
                "speaker": "NTIA Lead",
                "external_references": ["OB3"],
                "evidence_in_transcript": ["t0007"],
                "claim_complexity": "atomic",
                **_verbatim_grounding(quote),
            }
        ],
        "cross_references": [
            {
                "ref_id": "xref-1",
                "ref_type": "document",
                "ref_text": "the prior comment cycle",
                "ref_date": None,
                "ref_url": None,
                **_turn_aggregate_grounding(),
            }
        ],
        "attendees": [
            {
                "name": "Chair Smith",
                "agency": "FCC",
                "role": "Chair",
                "present": True,
                **_turn_aggregate_grounding(),
            }
        ],
        "topics": [
            {
                "topic_id": "t-1",
                "title": "7 GHz downlink power threshold",
                "start_timestamp": None,
                "end_timestamp": None,
                "summary": None,
                **_turn_aggregate_grounding(),
            }
        ],
        "regulatory_references": [
            {
                "ref_id": "reg-1",
                "reference_text": "47 CFR 96.41",
                "context": "cited as the operative power-limit rule",
                "speaker": "NTIA Lead",
                **_verbatim_grounding(quote),
            }
        ],
        "technical_parameters": [
            {
                "param_id": "p-1",
                "parameter_name": "7 GHz downlink threshold",
                "value": "minus 47 dBm per megahertz",
                "unit": "dBm/MHz",
                "context": "approved threshold",
                "speaker": "NTIA Lead",
                **_verbatim_grounding(quote),
            }
        ],
        "named_artifacts": [
            {
                "artifact_id": "art-1",
                "name": "Draft 7 GHz Study Plan",
                "artifact_type_description": "study",
                "url": None,
                "mentioned_by": "NTIA Lead",
                **_turn_aggregate_grounding(),
            }
        ],
        "scheduled_events": [
            {
                "event_id": "ev-1",
                "title": "next downlink TIG session",
                "date": "before the next session",
                "time": None,
                "location": None,
                "purpose": "review revised ERP values",
                **_turn_aggregate_grounding(),
            }
        ],
        "sentiment_indicators": [
            {
                "turn_id": "t0042",
                "speaker": "DoD Rep",
                "sentiment": "concern",
                "text_preview": "I am concerned about the timeline.",
                **_verbatim_grounding(quote),
            }
        ],
        "meeting_phases": [
            {
                "phase_id": "ph-1",
                "phase_name": "opening",
                "start_turn_id": "t0000",
                "end_turn_id": "t0004",
                "summary": None,
                **_turn_aggregate_grounding(),
            }
        ],
        "issue_registry_entry": [
            {
                "issue_id": "is-1",
                "title": "Aggregate interference modeling methodology",
                "description": "The TIG has not agreed on the propagation model.",
                "issue_type": "technical",
                "raised_by": "DoD Rep",
                "status": "open",
                "resolution_summary": None,
                "related_decisions": [],
                "source_turns": ["t0012"],
                **_verbatim_grounding(quote),
            }
        ],
        "position_statement": [
            {
                "position_id": "ps-1",
                "agency": "DoW",
                "speaker": "DoW Rep",
                "topic": "Classified parameters",
                "position_text": quote,
                "position_type": "opposition",
                "caveats": None,
                "source_turns": ["t0021"],
                **_verbatim_grounding(quote),
            }
        ],
        "dissent_or_objection": [
            {
                "dissent_id": "d-1",
                "objector": "NTIA Lead",
                "agency": "NTIA",
                "objection_text": quote,
                "objection_topic": "Adopting the threshold",
                "resolution": None,
                "resolved": False,
                "source_turns": ["t0044"],
                **_verbatim_grounding(quote),
            }
        ],
        "agenda_item": [
            {
                "item_id": "ag-1",
                "item_number": "Agenda Item 3",
                "title": "Study Plan Content Review",
                "presenter": "NTIA Lead",
                "allocated_minutes": 30,
                "start_turn_id": "t0030",
                "end_turn_id": "t0058",
                "outcome": None,
                **_turn_aggregate_grounding(),
            }
        ],
        "precedent_reference": [
            {
                "ref_id": "prec-1",
                "speaker": "Chair Smith",
                "reference_text": quote,
                "referenced_meeting_date": "2025-12-18",
                "referenced_decision_or_study": "December agreement",
                "purpose": "justification",
                "source_turns": ["t0009"],
                **_verbatim_grounding(quote),
            }
        ],
        "external_stakeholder_input": [
            {
                "input_id": "ext-1",
                "stakeholder": "CTIA",
                "relayed_by": "FCC Rep",
                "input_text": quote,
                "input_type": "industry_comment",
                "document_reference": "CTIA comment filing",
                "source_turns": ["t0037"],
                **_verbatim_grounding(quote),
            }
        ],
        "glossary_definition": [
            {
                "definition_id": "g-1",
                "term": "protection zone",
                "definition": "The area within which interference must be managed.",
                "defined_by": "NTIA Lead",
                "context": "Clarified before the protection-zone analysis discussion.",
                "authoritative": True,
                "source_turns": ["t0026"],
                **_verbatim_grounding(quote),
            }
        ],
        "procedural_ruling": [
            {
                "ruling_id": "ru-1",
                "ruling_text": quote,
                "ruled_by": "Chair Smith",
                "ruling_type": "scope_boundary",
                "binding": True,
                "source_turns": ["t0005"],
                **_verbatim_grounding(quote),
            }
        ],
        "grounding": [
            {"kind": k, "text": quote, "source_turns": ["t0007"]}
            for k in _CONTENT_ARRAYS
        ],
    }


def test_synthetic_response_has_all_22_content_arrays() -> None:
    """The fixture covers every content array the prompt enumerates."""
    response = _build_synthetic_response()
    for key in _CONTENT_ARRAYS:
        assert key in response, f"synthetic response missing array {key!r}"
        assert isinstance(response[key], list), (
            f"synthetic response array {key!r} is not a list"
        )
        assert len(response[key]) == 1, (
            f"synthetic response array {key!r} should carry one item"
        )


def test_synthetic_response_validates_against_meeting_minutes_schema() -> None:
    """The hand-crafted response passes the JSON Schema gate.

    This is the contract: the structure the new prompt instructs the
    model to emit is a structurally valid ``meeting_minutes`` artifact.
    The byte-match of ``source_quote`` against a real transcript is the
    next layer (``promotion/gate.py``) and is not exercised here.
    """
    response = _build_synthetic_response()
    validate_artifact(response, "meeting_minutes")


def test_synthetic_response_passes_strict_schema_eval() -> None:
    """The strict-schema eval that blocks `sonnet-unconstrained` passes.

    This is the exact eval that failed with
    ``failed:llm_extraction_strict_schema`` on the OLD Opus prompt.
    On the new prompt — i.e. on a response that carries the Phase 4a
    grounding fields per the schema's verbatim / turn-aggregate split —
    the eval must pass.
    """
    response = _build_synthetic_response()
    payload = {k: v for k, v in response.items() if k != "artifact_type"}
    artifact = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="opus-prompt-test",
    )
    result = run_llm_strict_schema_eval(artifact)
    assert result.artifact_type == "eval_result"
    result_payload = result.payload
    assert isinstance(result_payload, dict)
    assert result_payload.get("eval_type") == STRICT_SCHEMA_EVAL_TYPE
    assert result_payload.get("status") == "pass", (
        f"strict-schema eval failed on prompt-compliant payload: "
        f"reason_codes={result_payload.get('reason_codes')!r}"
    )


def test_verbatim_item_rejects_source_turn_ids() -> None:
    """Regression pin for the exact bug: ``source_turn_ids`` on a verbatim item.

    The OLD Opus prompt instructed ``source_turn_ids`` on every item,
    including verbatim types like ``decisions``. The verbatim
    sub-schemas declare ``additionalProperties: false`` and do NOT
    accept ``source_turn_ids``, so a literal-old-prompt artifact was
    rejected by the strict-schema gate. The new prompt segregates the
    two field sets; this test pins the schema-level rejection so the
    bug cannot regress silently.
    """
    response = _build_synthetic_response()
    # Mutate the one decisions item to add the wrong field — exactly
    # what the OLD prompt would have produced.
    response["decisions"][0]["source_turn_ids"] = [7]  # type: ignore[index]

    from spectrum_systems_core.validation import ArtifactValidationError

    with pytest.raises(ArtifactValidationError):
        validate_artifact(response, "meeting_minutes")


def test_turn_aggregate_item_rejects_source_quote() -> None:
    """The mirror case: a turn-aggregate item with verbatim fields fails.

    Equally important: the OLD prompt also told the model to put
    ``source_quote`` on every item, including turn-aggregate types
    like ``attendees`` / ``topics`` / ``cross_references``. Those
    sub-schemas reject ``source_quote`` the same way the verbatim
    sub-schemas reject ``source_turn_ids``. Pin the rejection so the
    cross-mode contamination cannot regress.
    """
    response = _build_synthetic_response()
    response["attendees"][0]["source_quote"] = "we will proceed"  # type: ignore[index]

    from spectrum_systems_core.validation import ArtifactValidationError

    with pytest.raises(ArtifactValidationError):
        validate_artifact(response, "meeting_minutes")
