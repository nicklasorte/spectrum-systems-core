"""Parser carry-through for the nine PR #123 structured arrays + the
Step 6 stakeholders/confidence fields, with the embedded red-team
rejection scenarios.

Trust properties defended here:

* every new array reaches the promoted artifact, defaulting to ``[]``
  (never ``null``, never absent) when the model omits it;
* the structured ``oneOf`` object form of action_items / open_questions
  / decisions is preserved verbatim — never silently coerced to a
  string;
* schema validation runs BEFORE the artifact is written (a violation
  blocks promotion, so a malformed payload never lands under
  ``processed/``);
* the Step 4 structured within-source gate and the Step 5 proxy
  nonempty gate fail closed.
"""
from __future__ import annotations

import json

from spectrum_systems_core.evals import (
    EXTRACTION_EMPTY_PROXY_TYPES,
    EXTRACTION_NOT_IN_SOURCE,
    NONEMPTY_EVAL_TYPE,
    SCHEMA_VIOLATION,
    STRICT_SCHEMA_EVAL_TYPE,
    WITHIN_SOURCE_EVAL_TYPE,
    WITHIN_SOURCE_WARN_PREFIX,
)
from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
    load_fixture,
    text_stub,
)

DEC18 = load_fixture("dec18_transcript.txt")
PROCEDURAL = load_fixture("procedural_only.txt")

# Texts that are verbatim / normalized substrings of dec18_transcript.txt.
GROUNDED_COMMITMENT_TEXT = (
    "DoD will submit revised ERP values before the next session."
)
GROUNDED_RISK_TEXT = (
    "DoD has a concern about the aggregate interference methodology"
)

NEW_ARRAY_KEYS = (
    "commitments",
    "risks",
    "cross_references",
    "attendees",
    "topics",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
)


def _eval(result, eval_type):
    matches = [
        e for e in result.eval_results if e.payload.get("eval_type") == eval_type
    ]
    assert len(matches) == 1, f"expected exactly one {eval_type} eval_result"
    return matches[0].payload


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


def _grounded_commitment() -> dict:
    return {
        "commitment_id": "c-1",
        "owner": "DoD Rep",
        "commitment_text": GROUNDED_COMMITMENT_TEXT,
        "due": "before the next session",
        "source_speaker": "DoD Rep",
    }


def _grounded_risk() -> dict:
    return {
        "risk_id": "r-1",
        "risk_text": GROUNDED_RISK_TEXT,
        "raised_by": "DoD Rep",
        "severity": None,
        "mitigation_mentioned": None,
    }


def _named_artifact() -> dict:
    return {
        "artifact_id": "na-1",
        "name": "prior comment cycle",
        "artifact_type_description": "report",
        "url": None,
        "mentioned_by": "NTIA Lead",
    }


# ---- Step 3 gate: full schema validation, all nine arrays carried ------


def test_all_nine_arrays_carry_through_and_validate_and_promote():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            commitments=[_grounded_commitment()],
            risks=[_grounded_risk()],
            cross_references=[
                {"ref_id": "x-1", "ref_type": "document",
                 "ref_text": "the prior comment cycle",
                 "ref_date": None, "ref_url": None}
            ],
            attendees=[{"name": "Chair Smith", "agency": "FCC"}],
            topics=[{"topic_id": "t-1", "title": "7 GHz downlink threshold"}],
            regulatory_references=[
                {"ref_id": "rr-1", "reference_text": "47 CFR 96.41",
                 "context": "power-limit rule", "speaker": "NTIA Lead"}
            ],
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
            named_artifacts=[_named_artifact()],
            scheduled_events=[
                {"event_id": "e-1", "title": "Next TIG session",
                 "date": "before the next session"}
            ],
        ),
    )
    assert _eval(result, STRICT_SCHEMA_EVAL_TYPE)["status"] == "pass"
    assert result.promoted is True
    assert _decision(result) == "allow"
    payload = result.meeting_minutes.payload
    for key in NEW_ARRAY_KEYS:
        assert key in payload, f"{key} missing from artifact"
        assert payload[key] is not None, f"{key} is null"
        assert isinstance(payload[key], list)
        assert len(payload[key]) == 1, f"{key} not carried through"


def test_omitted_new_arrays_default_to_empty_list_not_null():
    """Model returns only the legacy arrays — every one of the nine new
    arrays must still be present as ``[]`` (never null, never absent)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    assert result.promoted is True
    payload = result.meeting_minutes.payload
    for key in NEW_ARRAY_KEYS:
        assert key in payload, f"{key} absent from artifact"
        assert payload[key] is not None, f"{key} is null, must be []"
        assert isinstance(payload[key], list)
    # Only technical_parameters was supplied; the other eight default [].
    for key in (set(NEW_ARRAY_KEYS) - {"technical_parameters"}):
        assert payload[key] == [], f"{key} should default to []"


# ---- Red team: explicit null on a new array must block, not be patched -


def test_explicit_null_new_array_blocks_schema_violation():
    raw = json.dumps(
        {
            "decisions": DEC18_DECISIONS,
            "action_items": DEC18_ACTION_ITEMS,
            "open_questions": DEC18_OPEN_QUESTIONS,
            "technical_parameters": DEC18_TECHNICAL_PARAMETERS,
            "commitments": None,  # explicit null — must NOT become []
        }
    )
    result = run_meeting_minutes_llm_workflow(DEC18, client=text_stub(raw))
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(rc.startswith(SCHEMA_VIOLATION) for rc in strict["reason_codes"])
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


# ---- Red team: structured object preserved, never coerced to string ----


def test_structured_action_item_object_preserved_not_coerced():
    structured = {
        "action": GROUNDED_COMMITMENT_TEXT,
        "status": "in_progress",
        "owner": "DoD Rep",
        "due": "before the next session",
    }
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=[structured],
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    assert result.promoted is True
    item = result.meeting_minutes.payload["action_items"][0]
    assert isinstance(item, dict), "structured action_item was coerced!"
    assert item == structured


def test_malformed_structured_action_item_blocks_schema_violation():
    """An object action_item missing the required `action` fails the
    schema `oneOf` -> schema_violation -> block (not patched)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=[{"status": "open"}],  # missing required `action`
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(rc.startswith(SCHEMA_VIOLATION) for rc in strict["reason_codes"])
    assert _decision(result) == "block"
    assert result.promoted is False


# ---- Step 6: structured decision with stakeholders + confidence E2E ----


def test_structured_decision_with_stakeholders_confidence_promotes():
    decision_obj = {
        "text": DEC18_DECISIONS[0],
        "verb": "approved",
        "stakeholders": ["NTIA", "DoD"],
        "confidence": 0.92,
    }
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[decision_obj],
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
    carried = result.meeting_minutes.payload["decisions"][0]
    assert carried == decision_obj  # stakeholders + confidence preserved
    # The object-form decision was within-source checked, not bypassed.
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "pass"


# ---- Step 4 gate: structured within-source attribution -----------------


def test_step4_structured_within_source_happy():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            commitments=[_grounded_commitment()],
            risks=[_grounded_risk()],
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "pass"
    assert within["items_not_in_source"] == 0
    # 4 legacy + commitment_text + risk_text + tech_param.value = 7
    assert within["items_in_source"] == 7
    assert result.promoted is True


def test_step4_commitment_text_not_in_transcript_demotes_to_warn():
    """``commitments`` is a STANDARD lane type. A commitment_text that
    is not in the transcript is still DETECTED and RECORDED by the
    within_source eval, but for the STANDARD lane it is DEMOTED to a
    logged warn (promote, but record it) instead of hard-blocking —
    the demote-within-source-warn mission. The eval still runs and the
    miss is still measured; only the gate outcome changes for STANDARD.
    The HIGH_STAKES hard block is proven elsewhere
    (tests/test_within_source_routing.py)."""
    bad_commitment = _grounded_commitment()
    bad_commitment["commitment_text"] = "DoD will colonize the moon by Friday."
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            commitments=[bad_commitment],
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    # The eval still ran and still measured the miss (not silently
    # dropped) — only the gate outcome is softened for the STANDARD
    # lane.
    assert within["status"] == "warn"
    assert within["items_not_in_source"] >= 1
    assert any(
        rc.startswith(WITHIN_SOURCE_WARN_PREFIX)
        for rc in within["reason_codes"]
    )
    assert any("commitments" in rc for rc in within["reason_codes"])
    # The block-causing prefix is gone — it is logged, not blocking.
    assert not any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in within["reason_codes"]
    )
    assert _decision(result) == "allow"
    assert result.promoted is True
    assert result.meeting_minutes.status == "promoted"
    # The warn is recorded on provenance for the correction miner.
    warnings = result.meeting_minutes.payload["provenance"][
        "within_source_warnings"
    ]
    assert warnings and all(
        w.startswith(WITHIN_SOURCE_WARN_PREFIX) for w in warnings
    )


# ---- Step 5 gate: proxy-types nonempty ---------------------------------


def test_step5_rejection_all_proxy_types_empty_on_content_blocks():
    """Legacy arrays non-empty, but technical_parameters /
    named_artifacts / scheduled_events all empty on a content-bearing
    transcript -> block. Isolates the new proxy gate (the legacy
    extraction_empty_with_content code is NOT emitted)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
    )
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "fail"
    assert EXTRACTION_EMPTY_PROXY_TYPES in nonempty["reason_codes"]
    # Legacy arrays are populated, so the legacy-empty code is absent.
    assert "extraction_empty_with_content" not in nonempty["reason_codes"]
    assert _decision(result) == "block"
    assert result.promoted is False


def test_step5_proxy_satisfied_by_named_artifacts_only():
    """The proxy gate is an OR: named_artifacts alone (no
    technical_parameters, no scheduled_events) satisfies it."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            named_artifacts=[_named_artifact()],
        ),
    )
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "pass"
    assert result.promoted is True


def test_step5_procedural_transcript_still_promotes_empty():
    """No-content transcript: empty everything is allowed (never
    invent). The proxy gate must NOT fire when content is absent."""
    result = run_meeting_minutes_llm_workflow(PROCEDURAL, client=json_stub())
    assert result.meeting_minutes.payload["commitments"] == []
    assert _eval(result, NONEMPTY_EVAL_TYPE)["status"] == "pass"
    assert result.promoted is True
    assert _decision(result) == "allow"
