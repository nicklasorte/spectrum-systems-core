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


# ---- schema_version 1.2.0 additive fields ------------------------------
#
# Trust property: every 1.2.0 field is optional, so a 1.0.0 / 1.1.0
# artifact validates unchanged; a present-but-malformed value (enum
# miss, wrong type) is rejected fail-closed, never silently accepted.


def _claim(**overrides) -> dict:
    base = {
        "claim_id": "claim-1",
        "claim_text": "The 7 GHz downlink threshold protects federal incumbents.",
    }
    base.update(overrides)
    return base


def _sentiment(**overrides) -> dict:
    base = {
        "turn_id": "t0042",
        "speaker": "DoD Rep",
        "sentiment": "disagreement",
        "text_preview": "I strongly object to this approach for the 7 GHz downlink threshold.",
    }
    base.update(overrides)
    return base


def _phase(**overrides) -> dict:
    base = {
        "phase_id": "phase-1",
        "phase_name": "opening",
        "start_turn_id": "t0000",
        "end_turn_id": "t0004",
        "summary": "Roll call and agenda review.",
    }
    base.update(overrides)
    return base


def test_schema_version_enum_has_all_four():
    """1.3.0 sits in the enum alongside 1.0.0, 1.1.0 and 1.2.0
    (additivity: every legacy version string still validates; an
    unknown one does not)."""
    schema = _load_schema(ARTIFACT_TYPE)
    assert schema["properties"]["schema_version"]["enum"] == [
        "1.0.0",
        "1.1.0",
        "1.2.0",
        "1.3.0",
    ]


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0"])
@pytest.mark.parametrize(
    "fixture_dir",
    sorted(p for p in GOLDEN_DIR.iterdir() if p.is_dir())
    if GOLDEN_DIR.is_dir()
    else [],
    ids=lambda p: p.name,
)
def test_golden_transcripts_validate_on_all_four_schema_versions(
    fixture_dir, version
):
    """Schema additivity proof: every golden transcript's real regex
    payload validates when its schema_version is set to 1.0.0, 1.1.0,
    1.2.0, OR 1.3.0 — no legacy artifact breaks on the 1.3.0 schema."""
    transcript = (fixture_dir / "transcript.txt").read_text(encoding="utf-8")
    payload = run_meeting_minutes_workflow(transcript).meeting_minutes.payload
    art = _flat(payload)
    art["schema_version"] = version
    validate_artifact(art, ARTIFACT_TYPE)


# rationale (decisions) --------------------------------------------------


def test_decision_rationale_null_validates():
    art = _fully_populated()
    art["decisions"] = [_decision_object(rationale=None)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_decision_rationale_string_validates():
    art = _fully_populated()
    art["decisions"] = [_decision_object(rationale="to align with the OB3 mandate")]
    validate_artifact(art, ARTIFACT_TYPE)


def test_decision_rationale_wrong_type_fails():
    art = _fully_populated()
    art["decisions"] = [_decision_object(rationale=["not", "a", "string"])]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# claims.external_references / claims.evidence_in_transcript -------------


def test_claim_external_references_empty_validates():
    art = _fully_populated()
    art["claims"] = [_claim(external_references=[])]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_external_references_populated_validates():
    art = _fully_populated()
    art["claims"] = [_claim(external_references=["ITU Art 21"])]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_evidence_in_transcript_validates():
    art = _fully_populated()
    art["claims"] = [_claim(evidence_in_transcript=["t0001", "t0002"])]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_minimal_required_only_validates():
    """external_references / evidence_in_transcript are optional — a
    claim with only the two required fields still validates."""
    art = _fully_populated()
    art["claims"] = [_claim()]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_missing_required_text_fails():
    art = _fully_populated()
    art["claims"] = [{"claim_id": "claim-1"}]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_claim_external_references_not_array_fails():
    art = _fully_populated()
    art["claims"] = [_claim(external_references="ITU Art 21")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_claim_unknown_key_fails():
    art = _fully_populated()
    art["claims"] = [_claim(smuggled="x")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# action_items.follow_up_required ---------------------------------------


@pytest.mark.parametrize("flag", [True, False])
def test_action_item_follow_up_required_validates(flag):
    art = _fully_populated()
    art["action_items"][1]["follow_up_required"] = flag
    validate_artifact(art, ARTIFACT_TYPE)


def test_action_item_follow_up_required_not_boolean_fails():
    art = _fully_populated()
    art["action_items"][1]["follow_up_required"] = "yes"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# sentiment_indicators (top-level) --------------------------------------


def test_sentiment_indicators_empty_validates():
    art = _fully_populated()
    art["sentiment_indicators"] = []
    validate_artifact(art, ARTIFACT_TYPE)


def test_sentiment_indicators_populated_validates():
    art = _fully_populated()
    art["sentiment_indicators"] = [_sentiment(sentiment="disagreement")]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "value",
    ["disagreement", "concern", "strong_endorsement", "uncertainty", "frustration"],
)
def test_sentiment_indicators_each_enum_value_validates(value):
    art = _fully_populated()
    art["sentiment_indicators"] = [_sentiment(sentiment=value)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_sentiment_indicator_value_outside_enum_fails():
    art = _fully_populated()
    art["sentiment_indicators"] = [_sentiment(sentiment="annoyed")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_sentiment_indicator_missing_required_field_fails():
    art = _fully_populated()
    bad = _sentiment()
    del bad["text_preview"]
    art["sentiment_indicators"] = [bad]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_sentiment_indicator_empty_text_preview_fails():
    """text_preview is minLength 1 so a schema-valid item is always a
    usable non-empty string downstream (Opus baseline never HALTs)."""
    art = _fully_populated()
    art["sentiment_indicators"] = [_sentiment(text_preview="")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# meeting_phases (top-level) --------------------------------------------


def test_meeting_phases_empty_validates():
    art = _fully_populated()
    art["meeting_phases"] = []
    validate_artifact(art, ARTIFACT_TYPE)


def test_meeting_phases_populated_validates():
    art = _fully_populated()
    art["meeting_phases"] = [_phase(phase_name="opening")]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "value", ["opening", "working_session", "q_and_a", "wrap_up", "other"]
)
def test_meeting_phase_each_enum_value_validates(value):
    art = _fully_populated()
    art["meeting_phases"] = [_phase(phase_name=value)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_meeting_phase_name_outside_enum_fails():
    art = _fully_populated()
    art["meeting_phases"] = [_phase(phase_name="lunch")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_meeting_phase_null_turn_ids_validate():
    art = _fully_populated()
    art["meeting_phases"] = [
        _phase(start_turn_id=None, end_turn_id=None, summary=None)
    ]
    validate_artifact(art, ARTIFACT_TYPE)


# word_level_timestamps (top-level) -------------------------------------


def test_word_level_timestamps_false_validates():
    art = _fully_populated()
    art["word_level_timestamps"] = False
    validate_artifact(art, ARTIFACT_TYPE)


def test_word_level_timestamps_true_validates():
    """The schema permits true (future diarized transcripts); the
    chunker emits false for the current docx inputs — see
    tests/data_lake for the chunker-side default assertion."""
    art = _fully_populated()
    art["word_level_timestamps"] = True
    validate_artifact(art, ARTIFACT_TYPE)


def test_word_level_timestamps_not_boolean_fails():
    art = _fully_populated()
    art["word_level_timestamps"] = "false"
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# additivity: a legacy 1.0.0 / 1.1.0 artifact with NONE of the 1.2.0
# fields still validates (the core additivity guarantee).


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0"])
def test_legacy_artifact_without_any_1_2_0_field_validates(version):
    legacy = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": version,
        "title": "Quarterly sync",
        "summary": "Team reviewed Q3 priorities.",
        "decisions": ["Approve Q3 roadmap."],
        "action_items": ["Draft SSC-002 scope."],
        "open_questions": ["Do we need a separate empty-transcript eval?"],
    }
    validate_artifact(legacy, ARTIFACT_TYPE)


# ====================================================================
# schema_version 1.3.0 — eight new optional cross-meeting arrays plus
# the optional claim_complexity field on claims items.
#
# Trust property: every 1.3.0 array is optional (absent validates on
# the 1.3.0 schema AND a legacy version string), and every present
# item is typed fail-closed (enum miss / unknown key / missing
# required field rejected, never silently accepted).
# ====================================================================


def _issue(**o) -> dict:
    base = {
        "issue_id": "issue-1",
        "title": "Aggregate interference modeling methodology",
        "description": "The TIG has not agreed on the propagation model "
        "for the 7 GHz downlink protection-zone analysis.",
        "issue_type": "technical",
        "raised_by": "DoD Rep",
        "status": "open",
    }
    base.update(o)
    return base


def _position(**o) -> dict:
    base = {
        "position_id": "pos-1",
        "agency": "DoW",
        "speaker": "DoW Rep",
        "topic": "Classified system parameters",
        "position_text": "DoW's position is that classified system "
        "parameters cannot be shared in this forum.",
        "position_type": "opposition",
    }
    base.update(o)
    return base


def _dissent(**o) -> dict:
    base = {
        "dissent_id": "dis-1",
        "objector": "NTIA Lead",
        "agency": "NTIA",
        "objection_text": "NTIA objects to adopting the threshold before "
        "the aggregate study is complete.",
        "objection_topic": "Adopting the 7 GHz downlink threshold",
    }
    base.update(o)
    return base


def _agenda(**o) -> dict:
    base = {"item_id": "ag-1", "title": "Study Plan Content Review"}
    base.update(o)
    return base


def _precedent(**o) -> dict:
    base = {
        "ref_id": "prec-1",
        "speaker": "Chair Smith",
        "reference_text": "as we agreed at the December working group "
        "meeting",
        "purpose": "justification",
    }
    base.update(o)
    return base


def _stakeholder(**o) -> dict:
    base = {
        "input_id": "ext-1",
        "stakeholder": "CTIA",
        "relayed_by": "FCC Rep",
        "input_text": "CTIA submitted comments saying the proposed "
        "protection zone is larger than necessary.",
        "input_type": "industry_comment",
    }
    base.update(o)
    return base


def _glossary(**o) -> dict:
    base = {
        "definition_id": "gl-1",
        "term": "protection zone",
        "definition": "The area within which interference must be "
        "managed to protect federal incumbents.",
        "defined_by": "NTIA Lead",
    }
    base.update(o)
    return base


def _ruling(**o) -> dict:
    base = {
        "ruling_id": "rul-1",
        "ruling_text": "This TIG is scoped to 7250-7400 MHz only.",
        "ruled_by": "Chair Smith",
        "ruling_type": "scope_boundary",
    }
    base.update(o)
    return base


_NEW_ARRAY_KEYS = (
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)


def _v130_all_populated() -> dict:
    art = _fully_populated()
    art["schema_version"] = "1.3.0"
    art["issue_registry_entry"] = [_issue()]
    art["position_statement"] = [_position()]
    art["dissent_or_objection"] = [_dissent()]
    art["agenda_item"] = [_agenda()]
    art["precedent_reference"] = [_precedent()]
    art["external_stakeholder_input"] = [_stakeholder()]
    art["glossary_definition"] = [_glossary()]
    art["procedural_ruling"] = [_ruling()]
    return art


# ---- additivity ----------------------------------------------------


def test_v130_all_new_arrays_empty_validates():
    art = _fully_populated()
    art["schema_version"] = "1.3.0"
    for key in _NEW_ARRAY_KEYS:
        art[key] = []
    validate_artifact(art, ARTIFACT_TYPE)


def test_v130_all_new_arrays_populated_validates():
    validate_artifact(_v130_all_populated(), ARTIFACT_TYPE)


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0"])
def test_new_arrays_present_validate_on_every_schema_version(version):
    """Additivity in the strict sense: the eight new arrays are allowed
    (but never required) on ALL declared versions — a 1.0.0 artifact
    that happens to carry them still validates, and absence never
    blocks. New fields allowed-but-not-required = no legacy breakage."""
    art = _v130_all_populated()
    art["schema_version"] = version
    validate_artifact(art, ARTIFACT_TYPE)


def test_legacy_minimal_artifact_validates_on_1_3_0_schema():
    """A bare 1.0.0 legacy artifact with NONE of the new arrays
    validates against the (now 1.3.0) schema unchanged."""
    legacy = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": "1.0.0",
        "title": "Quarterly sync",
        "summary": "Team reviewed Q3 priorities.",
        "decisions": ["Approve Q3 roadmap."],
        "action_items": ["Draft SSC-002 scope."],
        "open_questions": ["Anything left open?"],
    }
    validate_artifact(legacy, ARTIFACT_TYPE)


# ---- happy-path enum coverage --------------------------------------


@pytest.mark.parametrize(
    "status", ["open", "in_progress", "resolved", "deferred"]
)
def test_issue_registry_entry_each_status_validates(status):
    art = _v130_all_populated()
    art["issue_registry_entry"] = [_issue(status=status)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "issue_type",
    ["technical", "policy", "procedural", "regulatory", "coordination"],
)
def test_issue_registry_entry_each_type_validates(issue_type):
    art = _v130_all_populated()
    art["issue_registry_entry"] = [_issue(issue_type=issue_type)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "ptype",
    ["support", "opposition", "conditional", "neutral", "unclear"],
)
def test_position_statement_each_type_validates(ptype):
    art = _v130_all_populated()
    art["position_statement"] = [_position(position_type=ptype)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_dissent_resolved_false_validates():
    art = _v130_all_populated()
    art["dissent_or_objection"] = [_dissent(resolved=False)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_dissent_resolved_true_validates():
    art = _v130_all_populated()
    art["dissent_or_objection"] = [
        _dissent(resolved=True, resolution="Withdrawn after discussion.")
    ]
    validate_artifact(art, ARTIFACT_TYPE)


def test_agenda_item_item_number_null_validates():
    art = _v130_all_populated()
    art["agenda_item"] = [_agenda(item_number=None, presenter=None)]
    validate_artifact(art, ARTIFACT_TYPE)


def test_agenda_item_minimal_required_only_validates():
    art = _v130_all_populated()
    art["agenda_item"] = [{"item_id": "ag-2", "title": "Roll call"}]
    validate_artifact(art, ARTIFACT_TYPE)


def test_agenda_item_allocated_minutes_integer_validates():
    art = _v130_all_populated()
    art["agenda_item"] = [_agenda(allocated_minutes=30)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "purpose",
    ["justification", "contrast", "correction", "context", "unknown"],
)
def test_precedent_reference_each_purpose_validates(purpose):
    art = _v130_all_populated()
    art["precedent_reference"] = [_precedent(purpose=purpose)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "itype",
    [
        "industry_comment",
        "itu_submission",
        "congressional_direction",
        "agency_guidance",
        "public_comment",
        "other",
    ],
)
def test_external_stakeholder_input_each_type_validates(itype):
    art = _v130_all_populated()
    art["external_stakeholder_input"] = [_stakeholder(input_type=itype)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize("auth", [True, False])
def test_glossary_definition_authoritative_validates(auth):
    art = _v130_all_populated()
    art["glossary_definition"] = [_glossary(authoritative=auth)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize(
    "rtype",
    [
        "scope_boundary",
        "process_rule",
        "meeting_procedure",
        "participation_rule",
        "classification_handling",
        "other",
    ],
)
def test_procedural_ruling_each_type_validates(rtype):
    art = _v130_all_populated()
    art["procedural_ruling"] = [_ruling(ruling_type=rtype)]
    validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize("binding", [True, False])
def test_procedural_ruling_binding_validates(binding):
    art = _v130_all_populated()
    art["procedural_ruling"] = [_ruling(binding=binding)]
    validate_artifact(art, ARTIFACT_TYPE)


# ---- fail-closed rejection paths -----------------------------------


def test_issue_registry_entry_status_outside_enum_fails():
    art = _v130_all_populated()
    art["issue_registry_entry"] = [_issue(status="done")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_issue_registry_entry_missing_required_field_fails():
    art = _v130_all_populated()
    bad = _issue()
    del bad["raised_by"]
    art["issue_registry_entry"] = [bad]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_issue_registry_entry_unknown_key_fails():
    art = _v130_all_populated()
    art["issue_registry_entry"] = [_issue(smuggled="x")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_position_statement_position_type_outside_enum_fails():
    art = _v130_all_populated()
    art["position_statement"] = [_position(position_type="maybe")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_position_statement_missing_agency_fails():
    art = _v130_all_populated()
    bad = _position()
    del bad["agency"]
    art["position_statement"] = [bad]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_dissent_resolved_not_boolean_fails():
    art = _v130_all_populated()
    art["dissent_or_objection"] = [_dissent(resolved="no")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_precedent_reference_purpose_outside_enum_fails():
    art = _v130_all_populated()
    art["precedent_reference"] = [_precedent(purpose="because")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_external_stakeholder_input_type_outside_enum_fails():
    art = _v130_all_populated()
    art["external_stakeholder_input"] = [
        _stakeholder(input_type="rumor")
    ]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_glossary_definition_authoritative_not_boolean_fails():
    art = _v130_all_populated()
    art["glossary_definition"] = [_glossary(authoritative="yes")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_procedural_ruling_ruling_type_outside_enum_fails():
    art = _v130_all_populated()
    art["procedural_ruling"] = [_ruling(ruling_type="vibes")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


def test_new_array_empty_string_primary_text_fails():
    """Each new type's primary text field is minLength 1 so a
    schema-valid item is always a usable non-empty string downstream
    (the Opus reference baseline never HALTs on a valid item)."""
    art = _v130_all_populated()
    art["procedural_ruling"] = [_ruling(ruling_text="")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


# ---- claim_complexity (optional field on claims items) -------------


def test_claim_complexity_atomic_validates():
    art = _v130_all_populated()
    art["claims"] = [_claim(claim_complexity="atomic")]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_complexity_compound_validates():
    art = _v130_all_populated()
    art["claims"] = [_claim(claim_complexity="compound")]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_complexity_absent_validates():
    """claim_complexity is OPTIONAL — a claim that omits it (every
    legacy claim) validates unchanged."""
    art = _v130_all_populated()
    art["claims"] = [_claim()]
    validate_artifact(art, ARTIFACT_TYPE)


def test_claim_complexity_outside_enum_fails():
    art = _v130_all_populated()
    art["claims"] = [_claim(claim_complexity="wrong")]
    with pytest.raises(ArtifactValidationError):
        validate_artifact(art, ARTIFACT_TYPE)


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0"])
def test_claim_with_complexity_validates_on_every_version(version):
    art = _v130_all_populated()
    art["schema_version"] = version
    art["claims"] = [_claim(claim_complexity="atomic")]
    validate_artifact(art, ARTIFACT_TYPE)


# ---- Opus-baseline lock-step guard (mission-critical) --------------
#
# The mission runs the Opus reference baseline over all transcripts
# right after this schema change. ``create_opus_reference_baselines.
# extraction_types()`` derives the type list from THIS schema's array
# properties and HARD-RAISES ``unmapped_extraction_type`` for any
# array missing a ``_PRIMARY_TEXT_FIELD`` mapping. If that regressed,
# the Opus batch would crash before producing a single baseline. This
# test fails closed in-suite instead of post-CI.


def _import_scripts():
    import sys

    scripts = str(
        pathlib.Path(__file__).resolve().parents[1] / "scripts"
    )
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import compare_opus_haiku as cmp  # noqa: WPS433
    import create_opus_reference_baselines as crb  # noqa: WPS433

    return crb, cmp


def test_opus_extraction_types_cover_every_new_array():
    """extraction_types() must not raise and must include all eight
    new arrays — proves the Opus batch run will capture them."""
    crb, _ = _import_scripts()
    types = crb.extraction_types()  # raises if any array is unmapped
    for key in _NEW_ARRAY_KEYS:
        assert key in types, f"{key} not derived for the Opus baseline"


def test_new_arrays_mapped_in_both_primary_text_field_maps():
    """Both scripts' _PRIMARY_TEXT_FIELD maps must carry every new
    array, mapped to a schema-required minLength-1 string field so a
    schema-valid item never HALTs the baseline. The byte-equality of
    the two maps is asserted in test_compare_opus_haiku.py; here we
    assert the new keys are present and point at a required field."""
    crb, cmp = _import_scripts()
    schema = _load_schema(ARTIFACT_TYPE)
    for key in _NEW_ARRAY_KEYS:
        assert key in crb._PRIMARY_TEXT_FIELD, f"{key} unmapped (crb)"
        assert key in cmp._PRIMARY_TEXT_FIELD, f"{key} unmapped (cmp)"
        field = crb._PRIMARY_TEXT_FIELD[key]
        item = schema["properties"][key]["items"]
        assert field in item["required"], (
            f"{key} primary text field {field!r} must be schema-required"
        )
        assert item["properties"][field].get("minLength") == 1, (
            f"{key}.{field} must be minLength 1 so the baseline never "
            f"HALTs on a schema-valid item"
        )
