from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import run_required_evals
from spectrum_systems_core.workflows import run_decision_brief_workflow

SAMPLE_BRIEF = """Adopt SSC-002 second artifact type
CONTEXT: Constitution requires one envelope and one control model.
OPTION: Add decision_brief workflow alongside meeting_minutes.
OPTION: Add a JSONL persistence layer instead.
RECOMMENDATION: Add decision_brief first.
RATIONALE: Validates generality before introducing I/O complexity.
"""


def test_decision_brief_workflow_promotes_end_to_end():
    result = run_decision_brief_workflow(SAMPLE_BRIEF)

    assert result.promoted is True
    assert result.decision_brief.status == "promoted"
    assert result.decision_brief.artifact_type == "decision_brief"
    assert result.control_decision.payload["decision"] == "allow"


def test_decision_brief_workflow_creates_all_expected_artifact_types():
    result = run_decision_brief_workflow(SAMPLE_BRIEF)

    types = {a.artifact_type for a in result.store.list()}
    assert types == {
        "context_bundle",
        "decision_brief",
        "eval_result",
        "control_decision",
    }


def test_decision_brief_payload_contains_required_fields():
    result = run_decision_brief_workflow(SAMPLE_BRIEF)
    payload = result.decision_brief.payload

    for key in ("title", "context", "options", "recommendation", "rationale"):
        assert key in payload

    assert payload["recommendation"] == "Add decision_brief first."
    assert "Add decision_brief workflow alongside meeting_minutes." in payload["options"]
    assert "Add a JSONL persistence layer instead." in payload["options"]


def test_decision_brief_workflow_is_deterministic():
    a = run_decision_brief_workflow(SAMPLE_BRIEF)
    b = run_decision_brief_workflow(SAMPLE_BRIEF)
    assert a.decision_brief.content_hash == b.decision_brief.content_hash
    assert a.decision_brief.trace_id == b.decision_brief.trace_id


def test_missing_decision_brief_field_fails_required_eval():
    bad = new_artifact(
        "decision_brief",
        {
            "title": "x",
            "context": "y",
            "options": [],
            "recommendation": "z",
        },
        trace_id="t",
    )
    results = run_required_evals(bad)
    by_type = {r.payload["eval_type"]: r for r in results}
    rdb = by_type["required_decision_brief_fields"]
    assert rdb.payload["status"] == "fail"
    assert "missing_field:rationale" in rdb.payload["reason_codes"]


def test_control_function_is_shared_across_artifact_types():
    """Same decide_control + envelope handles both artifact types."""
    minutes = new_artifact(
        "meeting_minutes",
        {
            "title": "t", "summary": "s",
            "decisions": [], "action_items": [], "open_questions": [],
        },
        trace_id="t",
    )
    brief = new_artifact(
        "decision_brief",
        {
            "title": "t", "context": "c",
            "options": ["a"], "recommendation": "r", "rationale": "why",
        },
        trace_id="t",
    )

    minutes_decision = decide_control(minutes, run_required_evals(minutes))
    brief_decision = decide_control(brief, run_required_evals(brief))

    assert minutes_decision.payload["decision"] == "allow"
    assert brief_decision.payload["decision"] == "allow"
    assert minutes_decision.artifact_type == brief_decision.artifact_type == "control_decision"


def test_decision_brief_workflow_blocks_when_payload_invalid(monkeypatch):
    from spectrum_systems_core.workflows import decision_brief as wf

    monkeypatch.setattr(wf, "_extract_decision_brief", lambda _t: {"title": "x"})
    result = wf.run_decision_brief_workflow("anything")

    assert result.promoted is False
    assert result.decision_brief.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
