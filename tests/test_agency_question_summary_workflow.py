from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.evals import run_required_evals
from spectrum_systems_core.workflows import run_agency_question_summary_workflow

SAMPLE_INPUT = """FCC inquiry on band plan
The agency requested clarification on band sharing.
AGENCY: FCC
QUESTION: What is the proposed sharing rule for 3.5 GHz?
QUESTION: Is the existing protection framework still in force?
CITATION: 47 CFR 96.41
CITATION: NPRM 23-456
We will respond within 30 days.
"""


def test_agency_question_summary_workflow_promotes_end_to_end():
    result = run_agency_question_summary_workflow(SAMPLE_INPUT)

    assert result.promoted is True
    assert result.agency_question_summary.status == "promoted"
    assert result.agency_question_summary.artifact_type == "agency_question_summary"
    assert result.control_decision.payload["decision"] == "allow"


def test_agency_question_summary_creates_all_expected_artifacts():
    result = run_agency_question_summary_workflow(SAMPLE_INPUT)
    types = {a.artifact_type for a in result.store.list()}
    assert types == {
        "context_bundle",
        "agency_question_summary",
        "eval_result",
        "control_decision",
    }


def test_agency_question_summary_payload_shape():
    result = run_agency_question_summary_workflow(SAMPLE_INPUT)
    payload = result.agency_question_summary.payload

    assert payload["title"] == "FCC inquiry on band plan"
    assert payload["agency"] == "FCC"
    assert "What is the proposed sharing rule for 3.5 GHz?" in payload["question"]
    assert "47 CFR 96.41" in payload["citations"]
    assert "NPRM 23-456" in payload["citations"]
    assert "respond within 30 days" in payload["summary"]


def test_missing_agency_question_summary_field_fails_eval():
    bad = new_artifact(
        "agency_question_summary",
        {"title": "x", "agency": "a", "question": "q", "summary": "s"},
        trace_id="t",
    )
    results = run_required_evals(bad)
    by_type = {r.payload["eval_type"]: r for r in results}
    rfields = by_type["required_agency_question_summary_fields"]
    assert rfields.payload["status"] == "fail"
    assert "missing_field:citations" in rfields.payload["reason_codes"]


def test_agency_question_summary_workflow_blocks_when_invalid(monkeypatch):
    from spectrum_systems_core.workflows import agency_question_summary as wf

    monkeypatch.setattr(
        wf, "_extract_agency_question_summary", lambda _t: {"title": "x"}
    )
    result = wf.run_agency_question_summary_workflow("anything")

    assert result.promoted is False
    assert result.agency_question_summary.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"


# --- SSC-023: non-empty required-field hardening -------------------------


def _required_fields_eval(artifact):
    results = run_required_evals(artifact)
    by_type = {r.payload["eval_type"]: r for r in results}
    return by_type["required_agency_question_summary_fields"]


def test_empty_agency_fails_required_fields_eval():
    """Field present but empty must fail with empty_required_field:agency."""
    bad = new_artifact(
        "agency_question_summary",
        {
            "title": "Inquiry",
            "agency": "",
            "question": "What is the rule?",
            "summary": "s",
            "citations": [],
        },
        trace_id="t",
    )
    rfields = _required_fields_eval(bad)
    assert rfields.payload["status"] == "fail"
    assert "empty_required_field:agency" in rfields.payload["reason_codes"]


def test_whitespace_only_agency_fails_required_fields_eval():
    bad = new_artifact(
        "agency_question_summary",
        {
            "title": "Inquiry",
            "agency": "   ",
            "question": "q",
            "summary": "s",
            "citations": [],
        },
        trace_id="t",
    )
    rfields = _required_fields_eval(bad)
    assert rfields.payload["status"] == "fail"
    assert "empty_required_field:agency" in rfields.payload["reason_codes"]


def test_empty_question_fails_required_fields_eval():
    bad = new_artifact(
        "agency_question_summary",
        {
            "title": "Inquiry",
            "agency": "FCC",
            "question": "",
            "summary": "s",
            "citations": [],
        },
        trace_id="t",
    )
    rfields = _required_fields_eval(bad)
    assert rfields.payload["status"] == "fail"
    assert "empty_required_field:question" in rfields.payload["reason_codes"]


def test_missing_agency_field_still_fails_required_fields_eval():
    """Removing the field entirely keeps the existing missing_field code."""
    bad = new_artifact(
        "agency_question_summary",
        {
            "title": "Inquiry",
            "question": "q",
            "summary": "s",
            "citations": [],
        },
        trace_id="t",
    )
    rfields = _required_fields_eval(bad)
    assert rfields.payload["status"] == "fail"
    assert "missing_field:agency" in rfields.payload["reason_codes"]


def test_valid_agency_question_summary_still_passes():
    """A populated payload must still pass after the new check is added."""
    good = new_artifact(
        "agency_question_summary",
        {
            "title": "Inquiry",
            "agency": "FCC",
            "question": "What is the rule?",
            "summary": "s",
            "citations": ["47 CFR 96.41"],
        },
        trace_id="t",
    )
    rfields = _required_fields_eval(good)
    assert rfields.payload["status"] == "pass"
    assert rfields.payload["reason_codes"] == []


def test_agency_question_summary_workflow_blocks_when_no_agency_line():
    """End-to-end: a transcript without AGENCY: must not promote."""
    no_agency = """FCC inquiry on band plan
QUESTION: What is the proposed sharing rule for 3.5 GHz?
CITATION: 47 CFR 96.41
We will respond within 30 days.
"""
    result = run_agency_question_summary_workflow(no_agency)

    assert result.promoted is False
    assert result.agency_question_summary.status == "rejected"
    assert result.control_decision.payload["decision"] == "block"
    rfields = next(
        r for r in result.eval_results
        if r.payload["eval_type"] == "required_agency_question_summary_fields"
    )
    assert rfields.payload["status"] == "fail"
    assert "empty_required_field:agency" in rfields.payload["reason_codes"]
