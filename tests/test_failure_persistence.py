"""Tests for SSC-021: failure persistence + human-review eval case format.

Covers:
- write_learning_artifact paths and rules
- review_eval_candidate status semantics
- end-to-end golden seed: weak transcript -> failure -> candidate ->
  reviewed_eval_case (accepted) -> on disk
- byte-deterministic learning-artifact files
- rejected candidates do not become required evals
- unknown learning artifact types are rejected
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake import (
    ALLOWED_REVIEW_STATUSES,
    EVAL_CANDIDATES_SUBDIR,
    EVAL_CASE_CANDIDATE_TYPE,
    FAILURES_SUBDIR,
    FAILURE_RECORD_TYPE,
    LEARNING_ARTIFACT_TYPES,
    REVIEWED_EVALS_SUBDIR,
    REVIEWED_EVAL_CASE_FIELDS,
    REVIEWED_EVAL_CASE_TYPE,
    WriterError,
    candidate_eval_case_from_failure,
    eval_candidates_dir,
    failures_dir,
    is_eligible_for_regression,
    is_required_eval,
    record_failure,
    review_eval_candidate,
    reviewed_evals_dir,
    run_transcript_pipeline,
    write_learning_artifact,
)


FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"

VALID_METADATA = {
    "meeting_id": "m-fail-21",
    "title": "Fail seed case",
    "date": "2026-05-09",
    "source_type": "transcript",
}

# Speaker-labelled to clear the Phase Y chunker gate; no
# DECISION/ACTION/QUESTION lines so transcript_evidence still blocks.
WEAK_TRANSCRIPT = (
    "ALICE: Just a header line\n"
    "BOB: More prose without prefixes.\n"
)


def _seed_lake(tmp_path: Path, transcript: str = WEAK_TRANSCRIPT, metadata=None) -> None:
    metadata = metadata or VALID_METADATA
    d = tmp_path / "raw" / "meetings" / metadata["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _blocked_run(tmp_path):
    _seed_lake(tmp_path)
    return run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id=VALID_METADATA["meeting_id"],
        workflow_name="meeting_minutes",
    )


def _failure_then_candidate(result):
    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )
    cand = candidate_eval_case_from_failure(fr)
    return fr, cand


# --- write_learning_artifact: paths and content ---------------------------


def test_failure_record_writes_to_failures_subdir(tmp_path):
    result = _blocked_run(tmp_path)
    fr, _ = _failure_then_candidate(result)

    path = write_learning_artifact(tmp_path, fr)
    expected_dir = failures_dir(tmp_path, VALID_METADATA["meeting_id"])
    assert path.parent == expected_dir
    assert path.name == f"{fr.artifact_id}.json"
    assert path.is_file()

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["artifact_type"] == FAILURE_RECORD_TYPE
    assert body["payload"]["meeting_id"] == VALID_METADATA["meeting_id"]


def test_eval_case_candidate_writes_to_eval_candidates_subdir(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)

    path = write_learning_artifact(tmp_path, cand)
    expected_dir = eval_candidates_dir(tmp_path, VALID_METADATA["meeting_id"])
    assert path.parent == expected_dir
    assert path.name == f"{cand.artifact_id}.json"

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["artifact_type"] == EVAL_CASE_CANDIDATE_TYPE
    assert body["payload"]["review_status"] == "pending_human_review"


def test_reviewed_eval_case_accepted_writes_to_reviewed_evals_subdir(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "accepted", "Looks like a real bug.")

    path = write_learning_artifact(tmp_path, reviewed)
    expected_dir = reviewed_evals_dir(tmp_path, VALID_METADATA["meeting_id"])
    assert path.parent == expected_dir
    assert path.name == f"{reviewed.artifact_id}.json"

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["artifact_type"] == REVIEWED_EVAL_CASE_TYPE
    assert body["status"] == "evaluated"
    assert body["payload"]["human_review_status"] == "accepted"
    assert body["payload"]["reviewer_notes"] == "Looks like a real bug."
    assert body["payload"]["eval_case_id"] == reviewed.artifact_id


def test_reviewed_eval_case_rejected_writes_with_rejected_envelope(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "rejected", "Spurious; transcript was test data.")

    path = write_learning_artifact(tmp_path, reviewed)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "rejected"
    assert body["payload"]["human_review_status"] == "rejected"


def test_reviewed_eval_case_needs_revision_payload(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "needs_revision", "Need a clearer eval_type.")

    path = write_learning_artifact(tmp_path, reviewed)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "evaluated"
    assert body["payload"]["human_review_status"] == "needs_revision"


# --- writer rules ---------------------------------------------------------


def test_write_learning_artifact_rejects_unknown_type(tmp_path):
    bogus = new_artifact(
        "meeting_minutes",
        {"meeting_id": "m-x", "title": "x"},
        trace_id="t",
        status="evaluated",
    )
    with pytest.raises(WriterError, match="learning artifact"):
        write_learning_artifact(tmp_path, bogus)


def test_write_learning_artifact_rejects_promoted_product_type(tmp_path):
    # control_decision is a run-internal type, definitely not a learning type
    bogus = new_artifact(
        "control_decision",
        {"meeting_id": "m-x"},
        trace_id="t",
        status="evaluated",
    )
    with pytest.raises(WriterError, match="learning artifact"):
        write_learning_artifact(tmp_path, bogus)


def test_write_learning_artifact_requires_meeting_id(tmp_path):
    fr = new_artifact(
        FAILURE_RECORD_TYPE,
        {"workflow_name": "meeting_minutes"},  # no meeting_id
        trace_id="t",
        status="evaluated",
    )
    with pytest.raises(WriterError, match="meeting_id"):
        write_learning_artifact(tmp_path, fr)


def test_write_learning_artifact_routes_with_explicit_meeting_id(tmp_path):
    fr = new_artifact(
        FAILURE_RECORD_TYPE,
        {"workflow_name": "meeting_minutes"},
        trace_id="t",
        status="evaluated",
    )
    path = write_learning_artifact(tmp_path, fr, meeting_id="m-explicit")
    assert "m-explicit" in str(path)
    assert path.parent.name == FAILURES_SUBDIR


def test_write_learning_artifact_is_byte_deterministic(tmp_path, tmp_path_factory):
    fr = new_artifact(
        FAILURE_RECORD_TYPE,
        {"meeting_id": "m-1", "workflow_name": "meeting_minutes"},
        trace_id="t",
        status="evaluated",
    )
    # Pin id/timestamp so the bytes can match across two runs.
    fr.artifact_id = "fixed-id"
    fr.created_at = "2026-05-09T00:00:00+00:00"

    p1 = write_learning_artifact(tmp_path, fr)
    other = tmp_path_factory.mktemp("other")
    p2 = write_learning_artifact(other, fr)
    assert p1.read_bytes() == p2.read_bytes()


def test_write_learning_artifact_rejects_unsafe_artifact_id(tmp_path):
    """Red team S1: a hand-built artifact_id with a slash must be refused."""
    fr = new_artifact(
        FAILURE_RECORD_TYPE,
        {"meeting_id": "m-1", "workflow_name": "meeting_minutes"},
        trace_id="t",
        status="evaluated",
    )
    fr.artifact_id = "../../etc/passwd"
    with pytest.raises(WriterError, match="unsafe artifact_id"):
        write_learning_artifact(tmp_path, fr)

    fr.artifact_id = ""
    with pytest.raises(WriterError, match="unsafe artifact_id"):
        write_learning_artifact(tmp_path, fr)


def test_learning_subdirs_match_known_types():
    # Belt-and-braces: keep the table small and discoverable.
    assert LEARNING_ARTIFACT_TYPES == {
        FAILURE_RECORD_TYPE,
        EVAL_CASE_CANDIDATE_TYPE,
        REVIEWED_EVAL_CASE_TYPE,
    }
    assert {FAILURES_SUBDIR, EVAL_CANDIDATES_SUBDIR, REVIEWED_EVALS_SUBDIR} == {
        "failures",
        "eval_candidates",
        "reviewed_evals",
    }


# --- review_eval_candidate semantics --------------------------------------


def test_review_eval_candidate_required_fields_present(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "accepted", "ok")
    for field in REVIEWED_EVAL_CASE_FIELDS:
        assert field in reviewed.payload, f"missing field {field}"


def test_review_eval_candidate_invalid_status_fails(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    with pytest.raises(ValueError, match="invalid review status"):
        review_eval_candidate(cand, "approved", "")  # 'approved' isn't allowed
    with pytest.raises(ValueError, match="invalid review status"):
        review_eval_candidate(cand, "", "")


def test_review_eval_candidate_rejects_non_candidate(tmp_path):
    not_a_candidate = new_artifact(
        FAILURE_RECORD_TYPE,
        {"meeting_id": "m-1"},
        trace_id="t",
        status="evaluated",
    )
    with pytest.raises(ValueError, match=EVAL_CASE_CANDIDATE_TYPE):
        review_eval_candidate(not_a_candidate, "accepted", "")


def test_review_eval_candidate_allowed_statuses_match():
    assert ALLOWED_REVIEW_STATUSES == {"accepted", "rejected", "needs_revision"}


# --- non-authority guarantees --------------------------------------------


def test_rejected_candidate_does_not_become_required_eval(tmp_path):
    """A rejected reviewed_eval_case must never be used as required coverage."""
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "rejected", "noise")

    assert is_eligible_for_regression(reviewed) is False

    # Re-running the pipeline does not pick up the reviewed eval case.
    second = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id=VALID_METADATA["meeting_id"],
        workflow_name="meeting_minutes",
    )
    eval_types = {e.payload["eval_type"] for e in second.eval_results}
    assert REVIEWED_EVAL_CASE_TYPE not in eval_types
    # Same blocked outcome.
    assert second.promoted is False


def test_needs_revision_candidate_does_not_become_required_eval(tmp_path):
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "needs_revision", "needs a sharper expectation")
    assert is_eligible_for_regression(reviewed) is False


def test_accepted_candidate_is_eligible_for_regression(tmp_path):
    """Eligible != automatically required. This is the consent flag."""
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "accepted", "real failure")
    assert is_eligible_for_regression(reviewed) is True
    # And the candidate itself is still not a required eval.
    assert is_required_eval(cand) is False


# --- golden regression seed ----------------------------------------------


def test_golden_weak_seeds_failure_candidate_and_accepted_review(tmp_path):
    """End-to-end seed: weak transcript -> failure -> candidate -> accepted review.

    Mirrors the constitution arrow:
        failure -> failure_record -> eval_case_candidate
                -> reviewed_eval_case (accepted)
    """
    meeting_id = "m-golden-weak"
    src = FIXTURES / meeting_id
    dst = tmp_path / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")

    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id, workflow_name="meeting_minutes"
    )
    assert result.promoted is False
    assert result.control_decision.payload["decision"] == "block"

    fr = record_failure(
        target_artifact=result.target,
        eval_results=result.eval_results,
        control_decision=result.control_decision,
        transcript_input=result.transcript_input,
    )
    cand = candidate_eval_case_from_failure(fr)
    reviewed = review_eval_candidate(
        cand, "accepted", "Weak transcript should reliably block promotion."
    )

    fr_path = write_learning_artifact(tmp_path, fr)
    cand_path = write_learning_artifact(tmp_path, cand)
    rev_path = write_learning_artifact(tmp_path, reviewed)

    # All three persist under the same meeting directory.
    assert fr_path.parent.name == FAILURES_SUBDIR
    assert cand_path.parent.name == EVAL_CANDIDATES_SUBDIR
    assert rev_path.parent.name == REVIEWED_EVALS_SUBDIR
    assert fr_path.parent.parent == cand_path.parent.parent == rev_path.parent.parent

    # Reviewed eval case payload is human-meaningful and cross-linked.
    rev_body = json.loads(rev_path.read_text(encoding="utf-8"))["payload"]
    assert rev_body["source_candidate_id"] == cand.artifact_id
    assert rev_body["meeting_id"] == meeting_id
    assert rev_body["human_review_status"] == "accepted"
    assert "no_transcript_evidence" in rev_body["failure_reason"]

    # No promoted product was written for this meeting; only learning files.
    # source_record__<meeting_id>.json (Phase Y) is pipeline infrastructure
    # (the on-disk anchor for the source_turn_validity eval), not a product.
    processed_dir = tmp_path / "processed" / "meetings" / meeting_id
    product_files = [
        f for f in processed_dir.glob("*.json")
        if not f.name.startswith("manifest__")
        and not f.name.startswith("debug__")
        and not f.name.startswith("source_record__")
    ]
    assert product_files == []


def test_learning_artifacts_do_not_blur_into_product_artifact_dir(tmp_path):
    """Learning files live under subdirectories; not at the meeting dir top level."""
    result = _blocked_run(tmp_path)
    fr, cand = _failure_then_candidate(result)
    reviewed = review_eval_candidate(cand, "accepted", "")

    write_learning_artifact(tmp_path, fr)
    write_learning_artifact(tmp_path, cand)
    write_learning_artifact(tmp_path, reviewed)

    meeting_dir = tmp_path / "processed" / "meetings" / VALID_METADATA["meeting_id"]
    top_level_jsons = [p for p in meeting_dir.glob("*.json")]
    # Top-level JSONs are only manifest__/debug__/source_record__/promoted-product
    # files. source_record__ (Phase Y) is pipeline infrastructure for the
    # source_turn_validity eval and lives at the meeting top level so the
    # eval can locate it deterministically.
    for p in top_level_jsons:
        assert (
            p.name.startswith("manifest__")
            or p.name.startswith("debug__")
            or p.name.startswith("source_record__")
        ), f"unexpected top-level file: {p}"


# --- determinism guarantee for end-to-end seed ----------------------------


def test_reviewed_eval_case_file_is_byte_identical_across_writes(
    tmp_path, tmp_path_factory
):
    """Same candidate + same review inputs -> same bytes."""
    result = _blocked_run(tmp_path)
    _, cand = _failure_then_candidate(result)

    # Pin envelope identity so we are testing serialization determinism.
    cand.artifact_id = "cand-fixed"
    cand.created_at = "2026-05-09T00:00:00+00:00"

    r1 = review_eval_candidate(cand, "accepted", "stable note")
    r1.artifact_id = "rev-fixed"
    r1.created_at = "2026-05-09T00:00:00+00:00"
    r1.payload["eval_case_id"] = r1.artifact_id
    r1.payload["created_at"] = r1.created_at
    from spectrum_systems_core.artifacts import compute_content_hash
    r1.content_hash = compute_content_hash(r1.payload)

    r2 = review_eval_candidate(cand, "accepted", "stable note")
    r2.artifact_id = "rev-fixed"
    r2.created_at = "2026-05-09T00:00:00+00:00"
    r2.payload["eval_case_id"] = r2.artifact_id
    r2.payload["created_at"] = r2.created_at
    r2.content_hash = compute_content_hash(r2.payload)

    p1 = write_learning_artifact(tmp_path, r1)
    other = tmp_path_factory.mktemp("other")
    p2 = write_learning_artifact(other, r2)
    assert p1.read_bytes() == p2.read_bytes()
