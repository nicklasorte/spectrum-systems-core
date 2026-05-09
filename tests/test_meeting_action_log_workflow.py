from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.evals import run_required_evals
from spectrum_systems_core.workflows import run_meeting_action_log_workflow


SAMPLE_INPUT = """Q3 planning action log
MEETING_REF: meeting-2026-05-09
ACTION: Owner Alice ships SSC-002 docs
ACTION: Owner Bob drafts citation policy
ACTION: Owner Carol reviews eval coverage
"""


def test_meeting_action_log_workflow_promotes_end_to_end():
    result = run_meeting_action_log_workflow(SAMPLE_INPUT)

    assert result.promoted is True
    assert result.meeting_action_log.status == "promoted"
    assert result.meeting_action_log.artifact_type == "meeting_action_log"
    assert result.control_decision.payload["decision"] == "allow"


def test_meeting_action_log_creates_all_expected_artifacts():
    result = run_meeting_action_log_workflow(SAMPLE_INPUT)
    types = {a.artifact_type for a in result.store.list()}
    assert types == {
        "context_bundle",
        "meeting_action_log",
        "eval_result",
        "control_decision",
    }


def test_meeting_action_log_payload_shape():
    result = run_meeting_action_log_workflow(SAMPLE_INPUT)
    payload = result.meeting_action_log.payload

    assert payload["title"] == "Q3 planning action log"
    assert payload["meeting_ref"] == "meeting-2026-05-09"
    assert payload["actions"] == [
        "Owner Alice ships SSC-002 docs",
        "Owner Bob drafts citation policy",
        "Owner Carol reviews eval coverage",
    ]
    assert payload["open_count"] == 3


def test_missing_meeting_action_log_field_fails_eval():
    bad = new_artifact(
        "meeting_action_log",
        {"title": "x", "meeting_ref": "m", "actions": []},
        trace_id="t",
    )
    results = run_required_evals(bad)
    by_type = {r.payload["eval_type"]: r for r in results}
    rfields = by_type["required_meeting_action_log_fields"]
    assert rfields.payload["status"] == "fail"
    assert "missing_field:open_count" in rfields.payload["reason_codes"]


def test_meeting_action_log_workflow_blocks_when_invalid(monkeypatch):
    from spectrum_systems_core.workflows import meeting_action_log as wf

    monkeypatch.setattr(
        wf, "_extract_meeting_action_log", lambda _t: {"title": "x"}
    )
    result = wf.run_meeting_action_log_workflow("anything")

    assert result.promoted is False
    assert result.meeting_action_log.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
