"""All four artifact types share one envelope, one control function, one promotion path."""

from spectrum_systems_core.workflows import (
    run_agency_question_summary_workflow,
    run_decision_brief_workflow,
    run_meeting_action_log_workflow,
    run_meeting_minutes_workflow,
)

CASES = [
    (
        run_meeting_minutes_workflow,
        "meeting_minutes",
        """Sync\nDECISION: ship\nACTION: write\nQUESTION: timing?\n""",
    ),
    (
        run_decision_brief_workflow,
        "decision_brief",
        """Brief\nCONTEXT: c\nOPTION: a\nRECOMMENDATION: r\nRATIONALE: why\n""",
    ),
    (
        run_agency_question_summary_workflow,
        "agency_question_summary",
        """Inquiry\nAGENCY: FCC\nQUESTION: q?\nCITATION: 47 CFR 96\nsummary line\n""",
    ),
    (
        run_meeting_action_log_workflow,
        "meeting_action_log",
        """Log\nMEETING_REF: m-1\nACTION: do thing\n""",
    ),
]


def test_all_artifact_types_share_one_loop():
    for run, expected_type, sample in CASES:
        result = run(sample)
        target_attr = expected_type
        target = getattr(result, target_attr)

        assert target.artifact_type == expected_type
        assert target.status == "promoted"
        assert result.promoted is True
        assert result.control_decision.artifact_type == "control_decision"
        assert result.control_decision.payload["decision"] == "allow"
        assert result.context_bundle.artifact_type == "context_bundle"


def test_all_workflows_use_same_envelope_field_set():
    fields = {
        "artifact_id",
        "artifact_type",
        "schema_version",
        "status",
        "created_at",
        "trace_id",
        "input_refs",
        "content_hash",
        "payload",
    }
    for run, expected_type, sample in CASES:
        result = run(sample)
        target = getattr(result, expected_type)
        for f in fields:
            assert hasattr(target, f), f"{expected_type} missing {f}"
