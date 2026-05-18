from __future__ import annotations

from ..artifacts import Artifact, new_artifact
from .regulatory_verb import (
    DECISION_BEARING_ARTIFACT_TYPES as _VERB_GUARD_TYPES,
)
from .regulatory_verb import (
    run_regulatory_verb_eval,
)

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
    # Phase Y.1 — a malformed ceiling (missing the fields the gate and
    # the comparator read) blocks via the required-fields eval before
    # ceiling_minimum_counts even runs.
    "opus_ceiling": {
        "1.0.0": {
            "top_level": (
                "transcript_id",
                "model_id",
                "extracted_items",
                "per_type_counts",
                "transcript_keyword_hits",
            ),
            "per_item_keys": (),
            "eval_type": "required_opus_ceiling_fields",
        },
    },
    # Phase Y.3 — registering extraction_alignment_comparison as a
    # required-fields type means a comparison artifact missing
    # total_metrics / per_type_metrics fails the required-fields eval
    # (status fail -> decide_control blocks) instead of slipping past
    # the F1 thresholds because the field it gates on is absent.
    "extraction_alignment_comparison": {
        "1.0.0": {
            "top_level": (
                "transcript_id",
                "ceiling_artifact_id",
                "haiku_artifact_id",
                "alignment_contract_version",
                "per_type_metrics",
                "total_metrics",
                "aligned_pairs",
            ),
            "per_item_keys": (),
            "eval_type": "required_extraction_alignment_comparison_fields",
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


def _check_ceiling_minimum_counts(target: Artifact) -> Artifact:
    """Phase Y.1 gate. For every schema_type the transcript visibly
    discusses (``transcript_keyword_hits[type] is True``), the ceiling
    must have extracted at least one item of that type. A zero count
    against a keyword-hit type is a ceiling miss and fails closed with
    the offending types named in ``failed_types`` so the failure is
    explainable from the eval_result alone (CLAUDE.md self-review).
    """
    payload = target.payload
    hits = payload.get("transcript_keyword_hits")
    counts = payload.get("per_type_counts")
    reason_codes: list[str] = []
    failed_types: list[str] = []
    if not isinstance(hits, dict) or not isinstance(counts, dict):
        # Missing the very inputs the gate reads -> fail closed, never
        # pass by absence (red-team: bypassable gate via missing input).
        reason_codes.append("ceiling_missing_gate_inputs")
        eval_payload = {
            "eval_type": "ceiling_minimum_counts",
            "target_artifact_id": target.artifact_id,
            "status": "fail",
            "score": 0.0,
            "reason_codes": reason_codes,
            "failed_types": [],
        }
        return new_artifact(
            artifact_type="eval_result",
            payload=eval_payload,
            trace_id=target.trace_id,
            status="evaluated",
            input_refs=[target.artifact_id],
        )
    for schema_type, hit in sorted(hits.items()):
        if hit and int(counts.get(schema_type, 0)) < 1:
            failed_types.append(schema_type)
            reason_codes.append(f"ceiling_zero_for_keyword_hit:{schema_type}")
    passed = not failed_types
    eval_payload = {
        "eval_type": "ceiling_minimum_counts",
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
        "failed_types": failed_types,
    }
    return new_artifact(
        artifact_type="eval_result",
        payload=eval_payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


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
    # Phase Z.2: regulatory verb guard for decision-bearing artifacts.
    # Non decision-bearing types short-circuit to pass inside the eval,
    # so calling it unconditionally is safe — but we gate here so the
    # eval list stays tight for non-decision artifacts.
    if artifact.artifact_type in _VERB_GUARD_TYPES:
        results.append(run_regulatory_verb_eval(artifact))
    # Phase Y.1 — the ceiling minimum-counts gate runs in addition to
    # (not instead of) the required-fields eval above, so a ceiling
    # that is well-formed but empty for a discussed type still blocks.
    if artifact.artifact_type == "opus_ceiling":
        results.append(_check_ceiling_minimum_counts(artifact))
    return results
