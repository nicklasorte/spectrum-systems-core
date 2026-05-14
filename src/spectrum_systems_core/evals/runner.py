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

# Default schema version emitted by extractors that do not set one
# explicitly. Phase Y introduces ``"1.1.0"`` for artifacts grounded to
# specific transcript turns.
DEFAULT_SCHEMA_VERSION = "1.0.0"

# Phase Y — version-branched required-field spec. ``REQUIRED_FIELDS_BY_TYPE``
# maps ``artifact_type -> { schema_version_string -> field_spec }``.
#
# Each ``field_spec`` is a dict with the following keys:
#   - ``top_level``: tuple of payload field names that must be present
#   - ``per_item_keys``: tuple of (parent_key, child_key) pairs; for each
#     parent_key, every item in ``payload[parent_key]`` must have a
#     non-empty ``child_key`` field
#   - ``eval_type``: the eval_type string written on the eval_result
#
# Single version branch point — do not add version checks elsewhere.
# All other eval logic is version-agnostic: it reads
# ``payload.get("schema_version", DEFAULT_SCHEMA_VERSION)`` and looks the
# spec up here.
#
# Rollback note: reverting Phase Y means deleting the ``"1.1.0"`` entry
# below. Artifacts already written to disk under schema_version "1.1.0"
# become stranded but remain valid — they simply will not be produced by
# new runs, and the runner will fall back to the ``"1.0.0"`` spec on
# any artifact (any unknown version is mapped down to ``"1.0.0"``).
REQUIRED_FIELDS_BY_TYPE: dict[str, dict[str, dict]] = {
    "meeting_minutes": {
        "1.0.0": {
            "top_level": REQUIRED_MEETING_MINUTES_FIELDS,
            "per_item_keys": (),
            "eval_type": "required_meeting_minutes_fields",
        },
        "1.1.0": {
            "top_level": REQUIRED_MEETING_MINUTES_FIELDS + ("schema_version",),
            "per_item_keys": (("grounding", "source_turns"),),
            "eval_type": "required_meeting_minutes_fields",
        },
    },
    "decision_brief": {
        "1.0.0": {
            "top_level": REQUIRED_DECISION_BRIEF_FIELDS,
            "per_item_keys": (),
            "eval_type": "required_decision_brief_fields",
        },
        "1.1.0": {
            "top_level": REQUIRED_DECISION_BRIEF_FIELDS + ("schema_version",),
            "per_item_keys": (("grounding", "source_turns"),),
            "eval_type": "required_decision_brief_fields",
        },
    },
    "agency_question_summary": {
        "1.0.0": {
            "top_level": REQUIRED_AGENCY_QUESTION_SUMMARY_FIELDS,
            "per_item_keys": (),
            "eval_type": "required_agency_question_summary_fields",
        },
        "1.1.0": {
            "top_level": REQUIRED_AGENCY_QUESTION_SUMMARY_FIELDS
            + ("schema_version",),
            "per_item_keys": (("grounding", "source_turns"),),
            "eval_type": "required_agency_question_summary_fields",
        },
    },
    "meeting_action_log": {
        "1.0.0": {
            "top_level": REQUIRED_MEETING_ACTION_LOG_FIELDS,
            "per_item_keys": (),
            "eval_type": "required_meeting_action_log_fields",
        },
        "1.1.0": {
            "top_level": REQUIRED_MEETING_ACTION_LOG_FIELDS
            + ("schema_version",),
            "per_item_keys": (("grounding", "source_turns"),),
            "eval_type": "required_meeting_action_log_fields",
        },
    },
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


def _check_per_item_fields(
    target: Artifact, per_item_keys: tuple
) -> list[str]:
    """For each (parent_key, child_key) pair: every item in
    payload[parent_key] (if present and a list) must have a non-empty
    child_key. Items missing the child_key, or with an empty value, fail.
    """
    reasons: list[str] = []
    for parent_key, child_key in per_item_keys:
        items = target.payload.get(parent_key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                reasons.append(
                    f"item_not_dict:{parent_key}[{idx}]"
                )
                continue
            if child_key not in item:
                reasons.append(
                    f"missing_item_field:{parent_key}[{idx}].{child_key}"
                )
                continue
            if _is_empty_value(item[child_key]):
                reasons.append(
                    f"empty_item_field:{parent_key}[{idx}].{child_key}"
                )
    return reasons


def _check_required_fields(
    target: Artifact,
    eval_type: str,
    required: tuple[str, ...],
    non_empty: tuple[str, ...] = (),
    per_item_keys: tuple = (),
) -> Artifact:
    reason_codes: list[str] = [
        f"missing_field:{f}" for f in required if f not in target.payload
    ]
    for f in non_empty:
        if f in target.payload and _is_empty_value(target.payload[f]):
            reason_codes.append(f"empty_required_field:{f}")
    reason_codes.extend(_check_per_item_fields(target, per_item_keys))
    if reason_codes:
        return _eval_result(
            eval_type, target, passed=False, reason_codes=reason_codes
        )
    return _eval_result(eval_type, target, passed=True, reason_codes=[])


def _lookup_spec(artifact_type: str, schema_version: str) -> dict | None:
    """Single version branch point. Returns the field_spec for the
    requested artifact_type + schema_version, falling back to "1.0.0"
    when the requested version is not known (rollback-safe)."""
    by_version = REQUIRED_FIELDS_BY_TYPE.get(artifact_type)
    if by_version is None:
        return None
    if schema_version in by_version:
        return by_version[schema_version]
    return by_version.get(DEFAULT_SCHEMA_VERSION)


def run_required_evals(artifact: Artifact) -> list[Artifact]:
    results: list[Artifact] = [_check_non_empty_payload(artifact)]
    schema_version = artifact.payload.get(
        "schema_version", DEFAULT_SCHEMA_VERSION
    )
    spec = _lookup_spec(artifact.artifact_type, schema_version)
    if spec is not None:
        non_empty = NON_EMPTY_REQUIRED_FIELDS_BY_TYPE.get(
            artifact.artifact_type, ()
        )
        results.append(
            _check_required_fields(
                artifact,
                eval_type=spec["eval_type"],
                required=spec["top_level"],
                non_empty=non_empty,
                per_item_keys=spec["per_item_keys"],
            )
        )
    return results
