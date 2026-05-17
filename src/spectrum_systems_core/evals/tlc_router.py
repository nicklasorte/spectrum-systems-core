"""P8-A TLC (Type-Lane Classification) routing layer.

After Sonnet produces the full 21-type meeting_minutes extraction, this
router classifies every extracted item by its array key into one of two
trust lanes and runs the eval set appropriate to that lane, then folds
the per-lane outcomes into ONE combined ``eval_result`` artifact. The
existing control gate (``control.decide_control``) reads that combined
result unchanged — it only ever inspects ``payload["status"]``.

Lanes
-----

* ``HIGH_STAKES_TYPES`` — decisions, risks, regulatory_references,
  technical_parameters and the four cross-meeting accountability arrays.
  Run the FULL eval set: ``regulatory_verb`` + ``within_source`` +
  ``strict_schema`` + ``nonempty``. Same-or-stricter than before.
* ``STANDARD_TYPES`` — high-volume descriptive arrays. Run
  ``within_source`` + ``strict_schema`` only. ``regulatory_verb`` is
  skipped because it is decision-governing-verb specific and not
  applicable to these types; ``nonempty`` is the HIGH_STAKES content
  floor and is not part of the STANDARD subset.

Why this is additive, never subtractive
---------------------------------------

The router CALLS the unmodified eval functions and never reimplements a
weaker variant. It is appended to ``extra_evals`` AFTER the workflow's
existing four LLM evals and the global ``run_required_evals`` (which
already runs ``regulatory_verb`` for meeting_minutes) — none of those is
removed. The router can therefore only ADD a fail signal, never relax
one: a payload that blocked before still blocks. HIGH_STAKES types get
the same evals as before; STANDARD types get a subset that is still a
strict superset of "nothing" and is enforced fail-closed.

Fail-closed invariants
----------------------

* A non-dict payload → combined ``fail`` (``tlc_payload_not_object``).
* A content-bearing array key that is in NEITHER lane → combined
  ``fail`` (``tlc_unknown_extraction_type:<key>``). An unrecognised
  extraction type can never silently route as STANDARD. (The
  strict-schema eval's ``additionalProperties:false`` also blocks it;
  this is defence-in-depth so the router's own contract is explicit.)
* The two lane sets are asserted disjoint AND jointly exhaustive of
  every content array ``meeting_minutes_llm`` can emit at import time —
  a future array added to the workflow but not classified here is a
  hard import error, so a HIGH_STAKES type can never silently drop to
  STANDARD routing through an omission.
* Any routed sub-eval that fails makes the combined result fail. The
  combined eval never raises (mirrors every other eval's contract).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..artifacts import Artifact, new_artifact
from .llm_extraction import (
    run_llm_nonempty_eval,
    run_llm_strict_schema_eval,
    run_llm_within_source_eval,
)
from .regulatory_verb import run_regulatory_verb_eval

TLC_ROUTED_EVAL_TYPE = "tlc_routed_extraction"

# Reason-code prefixes (stable, machine-grepable).
TLC_PAYLOAD_NOT_OBJECT = "tlc_payload_not_object"
TLC_UNKNOWN_TYPE_PREFIX = "tlc_unknown_extraction_type:"
TLC_SUBEVAL_FAIL_PREFIX = "tlc_subeval_failed:"

# HIGH_STAKES — full eval set (regulatory_verb + within_source +
# strict_schema + nonempty). These are the accountability-bearing
# arrays where a wrong/ungrounded item is a trust failure, so they get
# the same-or-stricter treatment they got before P8-A.
HIGH_STAKES_TYPES: frozenset[str] = frozenset(
    {
        "decisions",
        "risks",
        "regulatory_references",
        "technical_parameters",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
    }
)

# STANDARD — within_source + strict_schema only. High-volume descriptive
# arrays for which the decision-verb gate is not applicable.
STANDARD_TYPES: frozenset[str] = frozenset(
    {
        "action_items",
        "attendees",
        "scheduled_events",
        "named_artifacts",
        "topics",
        "cross_references",
        "claims",
        "open_questions",
        "commitments",
        "glossary_definition",
        "procedural_ruling",
        "agenda_item",
        "meeting_phases",
        "sentiment_indicators",
    }
)

# Payload keys that are NOT extraction-content arrays. They are governed
# by the global strict-schema eval (and, for grounding, by
# source_turn_validity) and must NOT be treated as an unknown
# extraction type by the router.
_NON_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "artifact_type",
        "title",
        "summary",
        "schema_version",
        "provenance",
        "meeting_id",
        "word_level_timestamps",
        "grounding",
        "_llm_error",
        "_llm_raw",
    }
)

# The canonical set of every content array ``meeting_minutes_llm`` can
# emit (its ``_LEGACY_ARRAYS`` + ``_STRUCTURED_ARRAYS``). Re-declared
# here rather than imported to avoid an evals→workflows import cycle;
# ``tests/evals/test_tlc_router.py`` asserts this set is exactly equal
# to the workflow's emitted arrays so the two cannot drift silently.
_ALL_CONTENT_ARRAYS: frozenset[str] = frozenset(
    {
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "risks",
        "claims",
        "cross_references",
        "attendees",
        "topics",
        "regulatory_references",
        "technical_parameters",
        "named_artifacts",
        "scheduled_events",
        "sentiment_indicators",
        "meeting_phases",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "agenda_item",
        "precedent_reference",
        "external_stakeholder_input",
        "glossary_definition",
        "procedural_ruling",
    }
)

# Import-time fail-closed contract. A drift here is a hard error so a
# HIGH_STAKES type can never silently fall into STANDARD routing (or
# vanish from routing entirely) through a classification omission.
_OVERLAP = HIGH_STAKES_TYPES & STANDARD_TYPES
assert not _OVERLAP, f"TLC lanes overlap: {sorted(_OVERLAP)}"
_CLASSIFIED = HIGH_STAKES_TYPES | STANDARD_TYPES
assert _CLASSIFIED == _ALL_CONTENT_ARRAYS, (
    "TLC routing must classify exactly every meeting_minutes content "
    f"array. unclassified={sorted(_ALL_CONTENT_ARRAYS - _CLASSIFIED)} "
    f"extra={sorted(_CLASSIFIED - _ALL_CONTENT_ARRAYS)}"
)


def _present_nonempty(payload: dict[str, Any], key: str) -> bool:
    """A type is 'present' for routing only when its value is a
    non-empty list. ``[]`` / missing / a null carries no item to route
    (a null is still caught fail-closed by the strict-schema eval)."""
    value = payload.get(key)
    return isinstance(value, list) and len(value) > 0


def _eval_result(
    target: Artifact,
    *,
    passed: bool,
    reason_codes: list[str],
    extra_payload: dict[str, Any],
) -> Artifact:
    payload: dict[str, Any] = {
        "eval_type": TLC_ROUTED_EVAL_TYPE,
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
    }
    payload.update(extra_payload)
    return new_artifact(
        artifact_type="eval_result",
        payload=payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


def run_tlc_routed_eval(
    artifact: Artifact, *, transcript_text: str
) -> Artifact:
    """Classify the extraction by type lane, route to the per-lane eval
    set, and return ONE combined ``eval_result``.

    Combined ``status`` is ``fail`` iff any routed sub-eval failed, the
    payload is not an object, or an unknown content array is present.
    ``decide_control`` blocks on that ``fail`` exactly as for any other
    eval — fail-closed and unchanged.
    """
    payload = artifact.payload
    if not isinstance(payload, dict):
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[TLC_PAYLOAD_NOT_OBJECT],
            extra_payload={
                "routed_high_stakes": [],
                "routed_standard": [],
                "unknown_types": [],
                "evals_run": [],
                "evals_skipped_standard_only": [],
            },
        )

    reason_codes: list[str] = []

    # --- classify every content-bearing array key present -------------
    high_stakes_present: list[str] = []
    standard_present: list[str] = []
    unknown_present: list[str] = []
    for key, value in payload.items():
        if key in _NON_CONTENT_KEYS:
            continue
        if not isinstance(value, list) or len(value) == 0:
            # Empty / non-list non-content-keyed values carry no item to
            # route. A null/garbage shape is still blocked fail-closed
            # by the strict-schema sub-eval below.
            continue
        if key in HIGH_STAKES_TYPES:
            high_stakes_present.append(key)
        elif key in STANDARD_TYPES:
            standard_present.append(key)
        else:
            unknown_present.append(key)
            reason_codes.append(f"{TLC_UNKNOWN_TYPE_PREFIX}{key}")

    high_stakes_present.sort()
    standard_present.sort()
    unknown_present.sort()
    has_high_stakes = bool(high_stakes_present)

    # --- route to the per-lane eval set -------------------------------
    # strict_schema + within_source apply to BOTH lanes, so they always
    # run (covers the STANDARD bad-enum block and the HIGH_STAKES
    # missing-source block). regulatory_verb + nonempty are the
    # HIGH_STAKES-only additions to the full set.
    evals_run: list[str] = []
    evals_skipped: list[str] = []

    routed: list[tuple[str, Callable[[], Artifact]]] = [
        ("llm_extraction_strict_schema", lambda: run_llm_strict_schema_eval(
            artifact
        )),
        (
            "extraction_within_source_required",
            lambda: run_llm_within_source_eval(artifact, transcript_text),
        ),
    ]
    if has_high_stakes:
        routed.append(
            ("regulatory_verb", lambda: run_regulatory_verb_eval(artifact))
        )
        routed.append(
            (
                "llm_extraction_nonempty_required",
                lambda: run_llm_nonempty_eval(artifact, transcript_text),
            )
        )
    else:
        # STANDARD-only payload: regulatory_verb (decision-verb
        # specific) and nonempty (HIGH_STAKES content floor) are not in
        # the STANDARD subset. Recorded so the skip is auditable, not
        # silent — this is the "STANDARD missing verb → allow" path.
        evals_skipped = ["regulatory_verb", "llm_extraction_nonempty_required"]

    all_passed = not unknown_present  # unknown type already blocks
    for label, run in routed:
        try:
            sub = run()
            sub_payload = sub.payload if isinstance(sub.payload, dict) else {}
            sub_status = sub_payload.get("status")
            sub_reasons = sub_payload.get("reason_codes") or []
        except Exception as exc:  # noqa: BLE001 — eval never raises
            # A sub-eval that itself raised is a fail-closed block, not
            # a silent pass (mirrors every eval's fail-closed contract).
            all_passed = False
            reason_codes.append(
                f"{TLC_SUBEVAL_FAIL_PREFIX}{label}:raised:"
                f"{type(exc).__name__}"
            )
            evals_run.append(label)
            continue
        evals_run.append(label)
        if sub_status != "pass":
            all_passed = False
            reason_codes.append(f"{TLC_SUBEVAL_FAIL_PREFIX}{label}")
            for rc in sub_reasons:
                reason_codes.append(f"{label}:{rc}")

    return _eval_result(
        artifact,
        passed=all_passed,
        reason_codes=reason_codes,
        extra_payload={
            "routed_high_stakes": high_stakes_present,
            "routed_standard": standard_present,
            "unknown_types": unknown_present,
            "evals_run": evals_run,
            "evals_skipped_standard_only": evals_skipped,
        },
    )


__all__ = [
    "HIGH_STAKES_TYPES",
    "STANDARD_TYPES",
    "TLC_ROUTED_EVAL_TYPE",
    "TLC_PAYLOAD_NOT_OBJECT",
    "TLC_UNKNOWN_TYPE_PREFIX",
    "TLC_SUBEVAL_FAIL_PREFIX",
    "run_tlc_routed_eval",
]
