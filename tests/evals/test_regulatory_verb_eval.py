"""Tests for the Phase Z.2 regulatory verb classification eval."""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import (
    DECISIONS_FIELD_MISSING,
    REGULATORY_VERB_EVAL_TYPE,
    VERB_AMBIGUOUS_PREFIX,
    VERB_NOT_CLASSIFIED_PREFIX,
    run_regulatory_verb_eval,
    run_required_evals,
)


def _meeting_minutes(decisions: list | None, *, include_field: bool = True):
    payload: dict = {
        "title": "x",
        "summary": "x",
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.1.0",
    }
    if include_field:
        payload["decisions"] = decisions if decisions is not None else []
    return new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )


# ---- happy paths ---------------------------------------------------------


def test_canonical_verb_approved_passes():
    artifact = _meeting_minutes(
        [{"text": "The FCC approved the framework.", "verb": "approved"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["eval_type"] == REGULATORY_VERB_EVAL_TYPE
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_canonical_verb_deferred_passes():
    artifact = _meeting_minutes(
        [{"text": "NTIA deferred the review.", "verb": "deferred"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_ambiguous_verb_discussed_warns_but_passes():
    artifact = _meeting_minutes(
        [{"text": "The committee discussed the amendment.",
          "verb": "discussed"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    # The reason_codes carry the warn for operator visibility.
    assert any(
        r.startswith(VERB_AMBIGUOUS_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_non_decision_artifact_type_passes_immediately():
    artifact = new_artifact(
        artifact_type="agency_question_summary",
        payload={"question": "x"},
        trace_id="trace-test",
        status="draft",
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_verb_extracted_from_text_when_no_verb_field():
    # The eval falls back to scanning text for a taxonomy verb when the
    # ``verb`` field is absent. "approved" appears, so the eval passes.
    artifact = _meeting_minutes(
        [{"text": "The FCC approved the framework."}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"


def test_empty_decisions_list_passes():
    # Zero decisions is a pass for THIS eval. The required-field eval
    # owns the "decisions must exist" claim.
    artifact = _meeting_minutes([])
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"


# ---- rejection paths -----------------------------------------------------


def test_unrecognized_verb_blocks_with_verb_not_classified():
    artifact = _meeting_minutes(
        [{"text": "Someone mumbled at the meeting.", "verb": "mumbled"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}mumbled")
        for r in result.payload["reason_codes"]
    )


def test_unclassified_verb_routes_to_block_through_decide_control():
    """Fail-closed: a fail status must translate to a ``block``
    decision via the control function, not a silent pass."""
    artifact = _meeting_minutes(
        [{"text": "x", "verb": "wibbled"}]
    )
    verb_eval = run_regulatory_verb_eval(artifact)
    decision = decide_control(artifact, [verb_eval])
    assert decision.payload["decision"] == "block"
    assert any(
        f"failed:{REGULATORY_VERB_EVAL_TYPE}" in r
        for r in decision.payload["reason_codes"]
    )


def test_decision_with_no_verb_and_no_taxonomy_word_in_text_blocks():
    artifact = _meeting_minutes(
        [{"text": "Nothing actionable was said about chocolate."}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}__missing__")
        for r in result.payload["reason_codes"]
    )


def test_mixed_verbs_one_canonical_one_unclassified_blocks_on_unclassified():
    artifact = _meeting_minutes(
        [
            {"text": "FCC approved the framework.", "verb": "approved"},
            {"text": "Chair grumbled.", "verb": "grumbled"},
        ]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}grumbled")
        for r in result.payload["reason_codes"]
    )


def test_missing_decisions_field_blocks_with_specific_reason_code():
    artifact = _meeting_minutes(decisions=None, include_field=False)
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert DECISIONS_FIELD_MISSING in result.payload["reason_codes"]


def test_non_list_decisions_field_blocks():
    artifact = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x", "summary": "x",
            "decisions": "not a list",
            "action_items": [], "open_questions": [],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"


# ---- integration with run_required_evals -------------------------------


def test_run_required_evals_includes_regulatory_verb_for_meeting_minutes():
    artifact = _meeting_minutes(
        [{"text": "FCC approved.", "verb": "approved"}]
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE in eval_types


def test_run_required_evals_includes_regulatory_verb_for_decision_brief():
    artifact = new_artifact(
        artifact_type="decision_brief",
        payload={
            "title": "x", "context": "x", "options": [],
            "recommendation": "x", "rationale": "x",
            "decisions": [
                {"text": "FCC approved framework.", "verb": "approved"}
            ],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE in eval_types


def test_run_required_evals_skips_regulatory_verb_for_non_decision_types():
    artifact = new_artifact(
        artifact_type="agency_question_summary",
        payload={
            "title": "x", "agency": "FCC", "question": "?",
            "summary": "x", "citations": [],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE not in eval_types
