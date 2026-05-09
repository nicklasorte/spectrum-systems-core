"""Governed-learning seed.

Constitution section 10:
    failure -> failure_record -> eval_case_candidate -> reviewed eval_case
    -> regression suite

This module covers the first three arrows. Failures can be recorded, a
`failure_record` can produce an `eval_case_candidate`, and a human can
turn a candidate into a `reviewed_eval_case` via `review_eval_candidate`.
A `reviewed_eval_case` is NOT auto-promoted into the required eval set;
that final arrow is a separate, deliberate step in a later slice.

All records live as plain artifacts on the existing envelope, so the
control loop and writer rules apply unchanged.
"""
from __future__ import annotations

from typing import Any

from ..artifacts import Artifact, new_artifact

FAILURE_RECORD_TYPE = "failure_record"
EVAL_CASE_CANDIDATE_TYPE = "eval_case_candidate"
REVIEWED_EVAL_CASE_TYPE = "reviewed_eval_case"

ALLOWED_REVIEW_STATUSES: frozenset[str] = frozenset(
    {"accepted", "rejected", "needs_revision"}
)

REVIEWED_EVAL_CASE_FIELDS: tuple[str, ...] = (
    "eval_case_id",
    "source_candidate_id",
    "meeting_id",
    "artifact_type",
    "eval_type",
    "input_excerpt",
    "expected_behavior",
    "failure_reason",
    "human_review_status",
    "reviewer_notes",
    "created_at",
)


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


def review_eval_candidate(
    candidate_artifact: Artifact,
    status: str,
    reviewer_notes: str = "",
) -> Artifact:
    """Promote a candidate into a human-reviewed eval case.

    `status` must be one of `accepted`, `rejected`, or `needs_revision`.

    - `accepted`  -> envelope status `evaluated`, payload
                     `human_review_status="accepted"`. Eligible for use as a
                     regression fixture (a separate, explicit later step).
    - `rejected`  -> envelope status `rejected`, payload
                     `human_review_status="rejected"`. Stored, but never
                     becomes required eval coverage.
    - `needs_revision` -> envelope status `evaluated`, payload
                          `human_review_status="needs_revision"`. Stored,
                          but not eligible until re-reviewed.

    No status, including `accepted`, automatically inserts the eval case
    into the required eval set. That step is owned by a human-curated
    fixture loader, not by this function.
    """
    if candidate_artifact.artifact_type != EVAL_CASE_CANDIDATE_TYPE:
        raise ValueError(
            f"expected {EVAL_CASE_CANDIDATE_TYPE!r}, got "
            f"{candidate_artifact.artifact_type!r}"
        )
    if status not in ALLOWED_REVIEW_STATUSES:
        raise ValueError(
            f"invalid review status {status!r}; must be one of "
            f"{sorted(ALLOWED_REVIEW_STATUSES)}"
        )
    if not isinstance(reviewer_notes, str):
        raise ValueError(
            f"reviewer_notes must be a string, got {type(reviewer_notes)!r}"
        )

    cand = candidate_artifact.payload
    proposed = cand.get("proposed_eval_types") or []
    expected_codes = cand.get("expected_reason_codes") or []
    eval_type = proposed[0] if proposed else ""
    failure_reason = ", ".join(expected_codes)
    expected_behavior = (
        f"required eval {eval_type!r} should fail with reason codes "
        f"{list(expected_codes)} on this input"
        if eval_type
        else "no specific eval proposed"
    )

    payload: dict[str, Any] = {
        "source_candidate_id": candidate_artifact.artifact_id,
        "meeting_id": cand.get("meeting_id"),
        "artifact_type": cand.get("workflow_name"),
        "eval_type": eval_type,
        "input_excerpt": cand.get("input_excerpt", ""),
        "expected_behavior": expected_behavior,
        "failure_reason": failure_reason,
        "human_review_status": status,
        "reviewer_notes": reviewer_notes,
    }

    envelope_status = "rejected" if status == "rejected" else "evaluated"
    reviewed = new_artifact(
        artifact_type=REVIEWED_EVAL_CASE_TYPE,
        payload=payload,
        trace_id=candidate_artifact.trace_id,
        status=envelope_status,
        input_refs=[candidate_artifact.artifact_id],
    )
    # Mirror the envelope artifact_id into the payload so a human reading
    # the file can see the reviewed eval's identity inside the payload too.
    reviewed.payload["eval_case_id"] = reviewed.artifact_id
    reviewed.payload["created_at"] = reviewed.created_at
    # Re-hash because we just mutated the payload.
    from ..artifacts import compute_content_hash
    reviewed.content_hash = compute_content_hash(reviewed.payload)
    return reviewed


def is_eligible_for_regression(reviewed_eval_case: Artifact) -> bool:
    """Only `accepted` reviewed eval cases are eligible for regression use."""
    if reviewed_eval_case.artifact_type != REVIEWED_EVAL_CASE_TYPE:
        return False
    return reviewed_eval_case.payload.get("human_review_status") == "accepted"
