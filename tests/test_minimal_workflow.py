from spectrum_systems_core.workflows import run_meeting_minutes_workflow

SAMPLE_TRANSCRIPT = """Quarterly planning sync
Team reviewed Q3 priorities and budget.
DECISION: Approve Q3 spectrum-core roadmap.
ACTION: Draft SSC-002 slice scope.
QUESTION: Do we need a separate eval for empty transcripts?
"""


def test_workflow_promotes_valid_meeting_minutes_artifact_end_to_end():
    result = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)

    assert result.promoted is True
    assert result.meeting_minutes.status == "promoted"
    assert result.meeting_minutes.artifact_type == "meeting_minutes"
    assert result.control_decision.payload["decision"] == "allow"


def test_workflow_creates_all_expected_artifact_types():
    result = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)

    types = {a.artifact_type for a in result.store.list()}
    assert "context_bundle" in types
    assert "meeting_minutes" in types
    assert "eval_result" in types
    assert "control_decision" in types

    promoted = [
        a for a in result.store.list()
        if a.artifact_type == "meeting_minutes" and a.status == "promoted"
    ]
    assert len(promoted) == 1


def test_workflow_payload_contains_required_fields():
    result = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)
    payload = result.meeting_minutes.payload

    for key in ("title", "summary", "decisions", "action_items", "open_questions"):
        assert key in payload

    assert "Approve Q3 spectrum-core roadmap." in payload["decisions"]
    assert {"action": "Draft SSC-002 slice scope."} in payload["action_items"]
    assert "Do we need a separate eval for empty transcripts?" in payload["open_questions"]


def test_workflow_is_deterministic_for_same_input():
    a = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)
    b = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)
    assert a.meeting_minutes.content_hash == b.meeting_minutes.content_hash
    assert a.meeting_minutes.trace_id == b.meeting_minutes.trace_id


def test_workflow_context_bundle_payload_shape():
    result = run_meeting_minutes_workflow(SAMPLE_TRANSCRIPT)
    cb = result.context_bundle
    assert cb.artifact_type == "context_bundle"
    assert cb.payload["purpose"] == "meeting_minutes"
    assert cb.payload["source_text"] == SAMPLE_TRANSCRIPT
    assert cb.payload["assumptions"] == []
    assert cb.payload["source_refs"] == []


def test_workflow_blocks_when_meeting_minutes_payload_invalid(monkeypatch):
    from spectrum_systems_core.workflows import meeting_minutes as wf

    def empty_extract(_text: str) -> dict:
        return {}

    monkeypatch.setattr(wf, "_extract_meeting_minutes", empty_extract)
    result = wf.run_meeting_minutes_workflow("anything")

    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
