"""Minimal governed-learning seed.

Constitution section 10:
    failure -> failure_record -> eval_case_candidate -> reviewed eval_case
    -> regression suite

This module covers the first two arrows only. Failures can be recorded,
and a `failure_record` can produce an `eval_case_candidate`. Candidates
are NOT auto-promoted into required evals; that step is reserved for a
human-reviewed `eval_case` and is intentionally not implemented here.

Both records live as plain artifacts on the existing envelope, so the
control loop and writer rules apply unchanged.
"""
from __future__ import annotations

from typing import Any

from ..artifacts import Artifact, new_artifact

FAILURE_RECORD_TYPE = "failure_record"
EVAL_CASE_CANDIDATE_TYPE = "eval_case_candidate"


def record_failure(
    *,
    target_artifact: Artifact,
    eval_results: list[Artifact],
    control_decision: Artifact,
    transcript_input,
) -> Artifact:
    """Build a failure_record artifact for a blocked run.

    Returns an artifact with status `evaluated` (not promoted). The caller
    decides whether to persist it. The `payload.input` block exists so the
    artifact is reproducible without re-running the pipeline.
    """
    failed_evals = [
        {
            "eval_type": e.payload.get("eval_type"),
            "reason_codes": list(e.payload.get("reason_codes", [])),
        }
        for e in eval_results
        if e.payload.get("status") == "fail"
    ]
    payload: dict[str, Any] = {
        "meeting_id": transcript_input.meeting_id,
        "workflow_name": target_artifact.artifact_type,
        "target_artifact_id": target_artifact.artifact_id,
        "decision": control_decision.payload.get("decision"),
        "reason_codes": list(control_decision.payload.get("reason_codes", [])),
        "failed_evals": failed_evals,
        "input": {
            "transcript_hash": transcript_input.transcript_hash,
            "metadata_hash": transcript_input.metadata_hash,
            "source_type": transcript_input.source_type,
        },
    }
    return new_artifact(
        artifact_type=FAILURE_RECORD_TYPE,
        payload=payload,
        trace_id=target_artifact.trace_id,
        status="evaluated",
        input_refs=[target_artifact.artifact_id, control_decision.artifact_id],
    )


def candidate_eval_case_from_failure(failure_record: Artifact) -> Artifact:
    """Build an eval_case_candidate from a failure_record.

    The candidate proposes a future regression eval with the smallest
    self-explanatory shape: the failed eval types, the reason codes, and
    the meeting/run identity. The candidate's status is `evaluated`, not
    `promoted` — promotion to a required eval is a human decision that
    this module deliberately does not make.
    """
    if failure_record.artifact_type != FAILURE_RECORD_TYPE:
        raise ValueError(
            f"expected {FAILURE_RECORD_TYPE!r}, got {failure_record.artifact_type!r}"
        )
    fr = failure_record.payload
    payload = {
        "meeting_id": fr.get("meeting_id"),
        "workflow_name": fr.get("workflow_name"),
        "proposed_eval_types": sorted({
            e.get("eval_type") for e in fr.get("failed_evals", []) if e.get("eval_type")
        }),
        "expected_reason_codes": sorted({
            code
            for e in fr.get("failed_evals", [])
            for code in e.get("reason_codes", [])
        }),
        "source_failure_record_id": failure_record.artifact_id,
        "review_status": "pending_human_review",
    }
    return new_artifact(
        artifact_type=EVAL_CASE_CANDIDATE_TYPE,
        payload=payload,
        trace_id=failure_record.trace_id,
        status="evaluated",
        input_refs=[failure_record.artifact_id],
    )


def is_required_eval(candidate: Artifact) -> bool:
    """A candidate never qualifies as a required eval automatically.

    This function exists so callers can ask the question and get a clear
    "no" rather than silently treating a candidate as production.
    """
    return False
