from __future__ import annotations

from ..artifacts import Artifact, new_artifact

REQUIRED_MEETING_MINUTES_FIELDS: tuple[str, ...] = (
    "title",
    "summary",
    "decisions",
    "action_items",
    "open_questions",
)


def _eval_result(
    eval_type: str,
    target: Artifact,
    passed: bool,
    reason_codes: list[str],
) -> Artifact:
    payload = {
        "eval_type": eval_type,
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
    }
    return new_artifact(
        artifact_type="eval_result",
        payload=payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


def _check_non_empty_payload(target: Artifact) -> Artifact:
    payload = target.payload
    is_empty = not payload or all(
        (v is None) or (hasattr(v, "__len__") and len(v) == 0)
        for v in payload.values()
    )
    if is_empty:
        return _eval_result(
            "non_empty_payload", target, passed=False, reason_codes=["empty_payload"]
        )
    return _eval_result("non_empty_payload", target, passed=True, reason_codes=[])


def _check_required_meeting_minutes_fields(target: Artifact) -> Artifact:
    missing = [f for f in REQUIRED_MEETING_MINUTES_FIELDS if f not in target.payload]
    if missing:
        return _eval_result(
            "required_meeting_minutes_fields",
            target,
            passed=False,
            reason_codes=[f"missing_field:{f}" for f in missing],
        )
    return _eval_result(
        "required_meeting_minutes_fields",
        target,
        passed=True,
        reason_codes=[],
    )


def run_required_evals(artifact: Artifact) -> list[Artifact]:
    results: list[Artifact] = [_check_non_empty_payload(artifact)]
    if artifact.artifact_type == "meeting_minutes":
        results.append(_check_required_meeting_minutes_fields(artifact))
    return results
