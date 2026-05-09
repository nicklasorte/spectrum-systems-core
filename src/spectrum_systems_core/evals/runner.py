from __future__ import annotations

from ..artifacts import Artifact, new_artifact

REQUIRED_MEETING_MINUTES_FIELDS: tuple[str, ...] = (
    "title",
    "summary",
    "decisions",
    "action_items",
    "open_questions",
)

REQUIRED_DECISION_BRIEF_FIELDS: tuple[str, ...] = (
    "title",
    "context",
    "options",
    "recommendation",
    "rationale",
)

REQUIRED_AGENCY_QUESTION_SUMMARY_FIELDS: tuple[str, ...] = (
    "title",
    "agency",
    "question",
    "summary",
    "citations",
)

REQUIRED_MEETING_ACTION_LOG_FIELDS: tuple[str, ...] = (
    "title",
    "meeting_ref",
    "actions",
    "open_count",
)

REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[tuple[str, ...], str]] = {
    "meeting_minutes": (
        REQUIRED_MEETING_MINUTES_FIELDS,
        "required_meeting_minutes_fields",
    ),
    "decision_brief": (
        REQUIRED_DECISION_BRIEF_FIELDS,
        "required_decision_brief_fields",
    ),
    "agency_question_summary": (
        REQUIRED_AGENCY_QUESTION_SUMMARY_FIELDS,
        "required_agency_question_summary_fields",
    ),
    "meeting_action_log": (
        REQUIRED_MEETING_ACTION_LOG_FIELDS,
        "required_meeting_action_log_fields",
    ),
}

# Fields that must be present AND non-empty. Presence is already covered by
# REQUIRED_FIELDS_BY_TYPE; this layer adds the "field is empty" check so
# `agency: ""` no longer slips through as if `agency` were truly populated.
# Keep this list narrow: only fields whose absence makes the artifact a lie.
NON_EMPTY_REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "agency_question_summary": ("agency", "question"),
    "decision_brief": ("recommendation",),
}


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


def _is_empty_value(value) -> bool:
    """A field is empty if it is None, a blank/whitespace string, or an
    empty list/tuple/dict/set. Numbers (e.g. ``open_count: 0``) are never
    considered empty here — semantic emptiness for numeric fields needs
    its own rule and is out of scope for this check."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _check_required_fields(
    target: Artifact,
    eval_type: str,
    required: tuple[str, ...],
    non_empty: tuple[str, ...] = (),
) -> Artifact:
    reason_codes: list[str] = [
        f"missing_field:{f}" for f in required if f not in target.payload
    ]
    for f in non_empty:
        if f in target.payload and _is_empty_value(target.payload[f]):
            reason_codes.append(f"empty_required_field:{f}")
    if reason_codes:
        return _eval_result(
            eval_type, target, passed=False, reason_codes=reason_codes
        )
    return _eval_result(eval_type, target, passed=True, reason_codes=[])


def run_required_evals(artifact: Artifact) -> list[Artifact]:
    results: list[Artifact] = [_check_non_empty_payload(artifact)]
    spec = REQUIRED_FIELDS_BY_TYPE.get(artifact.artifact_type)
    if spec is not None:
        required, eval_type = spec
        non_empty = NON_EMPTY_REQUIRED_FIELDS_BY_TYPE.get(
            artifact.artifact_type, ()
        )
        results.append(
            _check_required_fields(artifact, eval_type, required, non_empty)
        )
    return results
