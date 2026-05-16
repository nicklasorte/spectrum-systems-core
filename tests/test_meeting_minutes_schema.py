"""Contract + fail-closed gate for the extended meeting_minutes schema.

The schema (`schemas/meeting_minutes.schema.json`) is a flat content
projection — exactly the pattern `meeting_extraction.schema.json` and
`eval_result.schema.json` use: `artifact_type` + string
`schema_version` + the content fields at top level. The "artifact"
validated here is therefore `{"artifact_type": "meeting_minutes",
**payload}`.

These tests defend two trust properties:

* additivity — every existing regex / LLM meeting_minutes payload
  (legacy `list[str]` decisions/action_items/open_questions, no new
  arrays) still validates, proven against the real golden transcripts;
* fail-closed typing — a wrong enum / wrong type / unknown key on any
  new field is rejected, not silently accepted.
"""
from __future__ import annotations

import pathlib

import pytest

from spectrum_systems_core.validation import (
    ArtifactValidationError,
    _load_schema,
    validate_artifact,
)
from spectrum_systems_core.workflows import run_meeting_minutes_workflow

ARTIFACT_TYPE = "meeting_minutes"
GOLDEN_DIR = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "golden_meetings"
)


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    _load_schema.cache_clear()
    yield
    _load_schema.cache_clear()


def _flat(payload: dict) -> dict:
    """Project a workflow payload into the flat validated artifact."""
    return {"artifact_type": ARTIFACT_TYPE, **payload}


def _fully_populated() -> dict:
    """A meeting_minutes artifact exercising every new array + the
    structured action_item / open_question forms, all correctly typed."""
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": "1.2.0",
        "title": "7 GHz Downlink TIG Kickoff",
        "summary": "Kickoff of the 7 GHz downlink technical interference group.",
        "decisions": [
            "The group approved the 7 GHz downlink threshold of minus 47 dBm per megahertz."
        ],
        "action_items": [
            "Bare legacy action string still allowed.",
            {
                "action": "DoD will submit revised ERP values before the next session.",
                "status": "in_progress",
                "owner": "DoD Rep",
                "due": "before the next session",
            },
        ],
        "open_questions": [
            "Legacy bare question string still allowed.",
            {
                "question_id": "q-1",
                "question_text": "What is the coordination distance for federal incumbents in the 7 GHz band?",
                "asked_by": "NTIA Lead",
                "category": "Coordination / Geography",
                "initial_response": None,
                "follow_up_action": None,
                "resolved": False,
            },
        ],
        "commitments": [
            {
                "commitment_id": "c-1",
                "owner": "DoD Rep",
                "commitment_text": "DoD will submit revised ERP values before the next session.",
                "due": "before the next session",
                "source_speaker": "DoD Rep",
            }
        ],
        "risks": [
            {
                "risk_id": "r-1",
                "risk_text": "Aggregate interference methodology may be unsound and needs revisiting.",
                "raised_by": "DoD Rep",
                "severity": "medium",
                "mitigation_mentioned": "Deferred pending further study.",
            },
            {
                "risk_id": "r-2",
                "risk_text": "Unscored severity is allowed as null.",
                "raised_by": "Chair Smith",
                "severity": None,
                "mitigation_mentioned": None,
            },
        ],
        "cross_references": [
            {
                "ref_id": "x-1",
                "ref_type": "document",
                "ref_text": "the prior comment cycle",
                "ref_date": None,
                "ref_url": None,
            }
        ],
        "attendees": [
            {
                "name": "Chair Smith",
                "agency": "FCC",
                "role": "Chair",
                "present": True,
            },
            {"name": "DoD Rep", "agency": "DoD", "role": None},
        ],
        "topics": [
            {
                "topic_id": "t-1",
                "title": "7 GHz downlink power threshold",
                "start_timestamp": None,
                "end_timestamp": None,
                "summary": "Threshold set to minus 47 dBm per megahertz.",
            }
        ],
        "regulatory_references": [
            {
                "ref_id": "rr-1",
                "reference_text": "47 CFR 96.41",
                "context": "Cited as the operative power-limit rule.",
                "speaker": "NTIA Lead",
            }
        ],
        "technical_parameters": [
            {
                "param_id": "p-1",
                "parameter_name": "7 GHz downlink power threshold",
                "value": "-47 dBm/MHz",
                "unit": "dBm/MHz",
                "context": "Approved threshold for the 7 GHz downlink band.",
                "speaker": "NTIA Lead",
            }
        ],
        "named_artifacts": [
            {
                "artifact_id": "na-1",
                "name": "Prior comment cycle record",
                "artifact_type_description": "report",
                "url": None,
                "mentioned_by": "NTIA Lead",
            }
        ],
        "scheduled_events": [
            {
                "event_id": "e-1",
                "title": "Next 7 GHz Downlink TIG session",
                "date": "before the next session",
                "time": None,
                "location": None,
                "purpose": "Review revised ERP values.",
            }
        ],
        "provenance": {"produced_by": "meeting_minutes_llm"},
        "meeting_id": "m-7ghz-20251218",
    }


# ---- additivity --------------------------------------------------------


def test_fully_populated_artifact_validates():
    validate_artifact(_fully_populated(), ARTIFACT_TYPE)


def test_legacy_minimal_artifact_validates():
    """An artifact with NONE of the new arrays and legacy list[str]
    content must still validate — schema additivity."""
    legacy = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": "1.0.0",
        "title": "Quarterly sync",
        "summary": "Team reviewed Q3 priorities.",
        "decisions": ["Approve Q3 roadmap."],
        "action_items": ["Draft SSC-002 scope."],
        "open_questions": ["Do we need a separate empty-transcript eval?"],
    }
    validate_artifact(legacy, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "fixture_dir",
    sorted(p for p in GOLDEN_DIR.iterdir() if p.is_dir())
    if GOLDEN_DIR.is_dir()
    else [],
    ids=lambda p: p.name,
)
def test_real_golden_transcripts_still_validate(fixture_dir):
    """Every existing golden transcript, run through the REAL regex
    workflow, still produces a payload that validates — both the
    1.0.0 (no chunks) and the 1.1.0 (chunked, grounding) paths."""
    transcript = (fixture_dir / "transcript.txt").read_text(encoding="utf-8")

    legacy = run_meeting_minutes_workflow(transcript)
    validate_artifact(_flat(legacy.meeting_minutes.payload), ARTIFACT_TYPE)

    chunks = [
        {"turn_id": f"turn-{i}", "text": line}
        for i, line in enumerate(transcript.splitlines())
        if line.strip()
    ]
    grounded = run_meeting_minutes_workflow(transcript, chunks=chunks)
    validate_artifact(_flat(grounded.meeting_minutes.payload), ARTIFACT_TYPE)


def test_llm_workflow_happy_path_payload_validates():
    """The live-LLM workflow's happy-path payload shape (provenance,
    string content arrays) also conforms to the extended schema."""
    from tests.llm_stub import (
        DEC18_ACTION_ITEMS,
        DEC18_DECISIONS,
        DEC18_OPEN_QUESTIONS,
        json_stub,
    )
    from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow

    transcript = (
        pathlib.Path(__file__).resolve().parent
        / "fixtures"
        / "llm_extraction"
        / "dec18_transcript.txt"
    ).read_text(encoding="utf-8")
    result = run_meeting_minutes_llm_workflow(
        transcript,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
        ),
        meeting_id="m-7ghz-20251218",
    )
    validate_artifact(_flat(result.meeting_minutes.payload), ARTIFACT_TYPE)


# ---- fail-closed typing ------------------------------------------------


def test_action_item_status_outside_enum_fails():
    art = _fully_populated()
    art["action_items"][1]["status"] = "done"  # not in the enum
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_risk_severity_outside_enum_fails():
    art = _fully_populated()
    art["risks"][0]["severity"] = "critical"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_attendee_present_not_boolean_fails():
    art = _fully_populated()
    art["attendees"][0]["present"] = "yes"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_cross_reference_ref_type_outside_enum_fails():
    art = _fully_populated()
    art["cross_references"][0]["ref_type"] = "email"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_technical_parameter_missing_required_value_fails():
    art = _fully_populated()
    del art["technical_parameters"][0]["value"]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_unknown_top_level_key_fails():
    art = _fully_populated()
    art["unexpected_field"] = ["smuggled"]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_missing_legacy_required_field_fails():
    art = _fully_populated()
    del art["decisions"]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# ---- Step 6: stakeholders + confidence on a structured decision --------


def _decision_object(**overrides) -> dict:
    base = {
        "text": "The group approved the 7 GHz downlink threshold.",
        "verb": "approved",
        "stakeholders": ["NTIA", "DoD"],
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_structured_decision_with_stakeholders_and_confidence_validates():
    art = _fully_populated()
    art["decisions"] = [_decision_object()]
    validate_artifact(art, ARTIFACT_TYPE)


def test_structured_decision_without_optional_fields_validates():
    """stakeholders and confidence are OPTIONAL — a structured decision
    carrying only the required `text` still validates (additivity)."""
    art = _fully_populated()
    art["decisions"] = [{"text": "The group deferred the methodology."}]
    validate_artifact(art, ARTIFACT_TYPE)


def test_structured_decision_confidence_null_validates():
    art = _fully_populated()
    art["decisions"] = [_decision_object(confidence=None)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_legacy_string_and_structured_decision_mix_validates():
    """A decisions array may mix legacy strings and structured objects
    — the schema `oneOf` keeps both forms valid in one list."""
    art = _fully_populated()
    art["decisions"] = ["Legacy string decision still allowed.", _decision_object()]
    validate_artifact(art, ARTIFACT_TYPE)


def test_decision_confidence_out_of_range_fails():
    art = _fully_populated()
    art["decisions"] = [_decision_object(confidence=1.5)]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_decision_stakeholders_not_array_fails():
    art = _fully_populated()
    art["decisions"] = [_decision_object(stakeholders="NTIA")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_decision_object_unknown_key_fails():
    art = _fully_populated()
    art["decisions"] = [_decision_object(smuggled="x")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_decision_object_missing_required_text_fails():
    art = _fully_populated()
    art["decisions"] = [{"verb": "approved", "confidence": 0.5}]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)
