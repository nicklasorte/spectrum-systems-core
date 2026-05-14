"""Tests for the minimal governed-learning seed (SSC-016)."""
import json

import pytest

from spectrum_systems_core.data_lake import (
    EVAL_CASE_CANDIDATE_TYPE,
    FAILURE_RECORD_TYPE,
    candidate_eval_case_from_failure,
    is_required_eval,
    record_failure,
    run_transcript_pipeline,
)


VALID_METADATA = {
    "meeting_id": "m-fail-1",
    "title": "Fail seed case",
    "date": "2026-05-09",
    "source_type": "transcript",
}

# Speaker-labelled so the Phase Y chunker gate (no_speaker_structure)
# does not pre-empt transcript_evidence. No DECISION/ACTION/QUESTION
# lines means transcript_evidence still blocks the run.
WEAK_TRANSCRIPT = (
    "ALICE: Just a header line\n"
    "BOB: More prose without prefixes.\n"
)
GOOD_TRANSCRIPT = (
    "Header\nDECISION: Yes.\nACTION: Do it.\nQUESTION: When?\n"
)


def _seed(tmp_path, transcript=WEAK_TRANSCRIPT, metadata=None):
    metadata = metadata or VALID_METADATA
    d = tmp_path / "raw" / "meetings" / metadata["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _blocked_run(tmp_path):
    _seed(tmp_path, transcript=WEAK_TRANSCRIPT)
    return run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=VALID_METADATA["meeting_id"], workflow_name="meeting_minutes"
    )


def test_failed_eval_can_create_failure_record(tmp_path):
    result = _blocked_run(tmp_path)
    assert result.promoted is False

    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )

    assert fr.artifact_type == FAILURE_RECORD_TYPE
    assert fr.status == "evaluated"
    assert fr.payload["meeting_id"] == "m-fail-1"
    assert fr.payload["decision"] == "block"
    assert any(
        "no_transcript_evidence" in e.get("reason_codes", [])
        for e in fr.payload["failed_evals"]
    )
    # Input identity is preserved so the failure is reproducible
    assert fr.payload["input"]["transcript_hash"] == result.transcript_input.transcript_hash


def test_failure_record_can_create_eval_case_candidate(tmp_path):
    result = _blocked_run(tmp_path)
    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )
    candidate = candidate_eval_case_from_failure(fr)

    assert candidate.artifact_type == EVAL_CASE_CANDIDATE_TYPE
    assert candidate.status == "evaluated"
    assert candidate.payload["source_failure_record_id"] == fr.artifact_id
    assert "transcript_evidence" in candidate.payload["proposed_eval_types"]
    assert "no_transcript_evidence" in candidate.payload["expected_reason_codes"]
    assert candidate.payload["review_status"] == "pending_human_review"


def test_eval_case_candidate_does_not_become_required_eval_automatically(tmp_path):
    """The whole point of the seed: candidates need a human."""
    result = _blocked_run(tmp_path)
    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )
    candidate = candidate_eval_case_from_failure(fr)

    assert is_required_eval(candidate) is False

    # And the candidate itself is not 'promoted' status.
    assert candidate.status == "evaluated"
    assert candidate.status != "promoted"

    # Re-running the pipeline does not pick up the candidate as a new eval.
    second = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=VALID_METADATA["meeting_id"], workflow_name="meeting_minutes"
    )
    eval_types = sorted({e.payload["eval_type"] for e in second.eval_results})
    # Same eval set as before — no new eval crept in.
    assert "transcript_evidence" in eval_types
    # Candidate type is not in eval results
    assert all(t != EVAL_CASE_CANDIDATE_TYPE for t in eval_types)


def test_candidate_from_non_failure_record_is_rejected():
    from spectrum_systems_core.artifacts import new_artifact
    not_a_failure = new_artifact(
        "meeting_minutes",
        {"title": "x"},
        trace_id="t",
        status="draft",
    )
    with pytest.raises(ValueError, match="failure_record"):
        candidate_eval_case_from_failure(not_a_failure)


def test_failure_record_for_passing_run_has_empty_failed_evals(tmp_path):
    """If we record a 'failure' on a successful run, failed_evals is empty."""
    _seed(tmp_path, transcript=GOOD_TRANSCRIPT)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=VALID_METADATA["meeting_id"], workflow_name="meeting_minutes"
    )
    assert result.promoted is True
    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )
    assert fr.payload["failed_evals"] == []
