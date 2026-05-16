"""``source_turn_validity`` eval — verifies every extracted item's
``source_turns`` reference a real chunk in the persisted ``source_record``.

Phase Y closes the source-verifiability gap: ``source_grounding`` and
``transcript_evidence`` check that *some* grounded spans exist, but they
do not verify that *specific extracted items* point to *specific
transcript locations*. This eval does that. A fabricated turn_id (one
that does not appear in any chunk of the source_record on disk) blocks
the artifact.

Key rules:

1. The source_record is read from disk every time. Never trust an
   in-memory representation passed in by the pipeline — the on-disk
   form is what downstream consumers will read. A missing, unreadable,
   or malformed source_record always fails the eval explicitly (it
   never passes silently).
2. Every extracted item with a ``source_turns`` field has every entry
   in that list checked against the valid turn_id set built from
   ``source_record.payload.chunks``. An unresolved turn_id fails.
3. ``source_record_invalid`` is the catch-all reason code for "the
   source_record on disk does not give us a usable valid_turn_ids
   set". It is emitted in any of these cases (each described below):
     - the path argument is None
     - the file does not exist
     - the file is not valid UTF-8 JSON
     - the JSON is not an object
     - the JSON object has no ``payload`` dict
     - ``payload.chunks`` is missing, not a list, or empty
     - any chunk in the list is not a dict or has no ``turn_id``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ..artifacts import Artifact, new_artifact


# Reason codes
SOURCE_RECORD_INVALID = "source_record_invalid"
CHUNKS_INVALID = "chunks_invalid"
SOURCE_TURN_UNRESOLVED_PREFIX = "source_turn_unresolved:"
SOURCE_MATCH_FALLBACK_PREFIX = "source_match_fallback:"

# Eval identifier
EVAL_TYPE = "source_turn_validity"

# Payload keys that carry lists of extracted items potentially bearing
# ``source_turns``. Keep this list narrow and explicit — silently
# skipping items because they live under a key not listed here would
# allow fabricated turn_ids to pass the gate.
ITEM_LIST_KEYS: tuple[str, ...] = ("grounding", "items")


def _load_source_record(
    source_record_path: Path | str | None,
) -> tuple[dict | None, str | None]:
    """Read and validate the source_record on disk.

    Returns ``(record, reason)``. ``record`` is the parsed JSON object
    when valid; ``reason`` is a short human-readable cause when not.
    Either ``record`` is non-None and ``reason`` is None, or vice versa.
    """
    if source_record_path is None:
        return None, "source_record_path is None"
    path = Path(source_record_path)
    if not path.is_file():
        return None, f"source_record file not found at {path}"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"source_record unreadable: {exc}"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"source_record is not valid JSON: {exc}"
    if not isinstance(record, dict):
        return None, "source_record is not a JSON object"
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None, "source_record.payload is not a dict"
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        return None, "source_record.payload.chunks is missing or not a list"
    if not chunks:
        return None, "source_record.payload.chunks is empty"
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            return None, f"source_record.payload.chunks[{i}] is not a dict"
        if "turn_id" not in chunk or not isinstance(chunk["turn_id"], str):
            return (
                None,
                f"source_record.payload.chunks[{i}] missing string turn_id",
            )
    return record, None


def _build_valid_turn_ids(record: dict) -> set[str]:
    return {c["turn_id"] for c in record["payload"]["chunks"]}


def _iter_items_with_source_turns(
    artifact: Artifact,
) -> Iterable[tuple[str, int, dict]]:
    """Yield (parent_key, item_index, item) for every dict-shaped item
    under one of the known item-list keys that carries a ``source_turns``
    field. The eval validates only items that have the field — items
    without it are caught by the required-field eval at schema_version
    1.1.0 (see runner.REQUIRED_FIELDS_BY_TYPE).
    """
    for parent_key in ITEM_LIST_KEYS:
        items = artifact.payload.get(parent_key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, dict) and "source_turns" in item:
                yield parent_key, idx, item


def _eval_result(
    target: Artifact,
    passed: bool,
    reason_codes: list[str],
    eval_type: str = EVAL_TYPE,
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


def _collect_unresolved_reason_codes(
    artifact: Artifact, valid_turn_ids: set[str]
) -> list[str]:
    """Pure core: build the ``source_turn_unresolved:`` reason codes for
    every item whose ``source_turns`` is non-list, empty, carries a
    non-string entry, or references a turn_id outside ``valid_turn_ids``.

    This is the single source-turn validation rule. Both the on-disk
    entry point (:func:`run_source_turn_validity_eval`) and the
    in-memory entry point (:func:`run_source_turn_validity_eval_from_chunks`)
    delegate here so there is exactly one validation logic and one
    control authority — the returned ``eval_result`` flows through the
    same ``decide_control`` gate regardless of how the valid turn-id
    set was built.
    """
    reason_codes: list[str] = []
    for parent_key, idx, item in _iter_items_with_source_turns(artifact):
        source_turns = item.get("source_turns")
        # Non-list source_turns is a fail — required-field eval treats
        # a non-empty string as "present" because _is_empty_value
        # returns False for it. If we skipped here, a payload with
        # ``"source_turns": "t0001"`` (string, not list) would pass
        # both gates silently. Fail loud instead.
        if not isinstance(source_turns, list):
            reason_codes.append(
                f"{SOURCE_TURN_UNRESOLVED_PREFIX}{parent_key}[{idx}]:"
                f"source_turns_not_a_list"
            )
            continue
        if not source_turns:
            reason_codes.append(
                f"{SOURCE_TURN_UNRESOLVED_PREFIX}{parent_key}[{idx}]:"
                f"empty_source_turns_list"
            )
            continue
        for turn_id in source_turns:
            if not isinstance(turn_id, str):
                reason_codes.append(
                    f"{SOURCE_TURN_UNRESOLVED_PREFIX}{parent_key}[{idx}]:"
                    f"non_string_turn_id"
                )
                continue
            if turn_id not in valid_turn_ids:
                reason_codes.append(
                    f"{SOURCE_TURN_UNRESOLVED_PREFIX}{parent_key}[{idx}]:"
                    f"{turn_id}"
                )
    return reason_codes


def _valid_turn_ids_from_chunks(
    chunks: object,
) -> tuple[set[str] | None, str | None]:
    """Build the valid turn-id set from an in-memory chunk list.

    Mirrors :func:`_load_source_record`'s fail-closed contract: an
    absent, non-list, empty, or malformed chunk list yields
    ``(None, reason)`` so the caller fails the eval explicitly rather
    than passing silently on missing input.
    """
    if not isinstance(chunks, list):
        return None, "chunks is missing or not a list"
    if not chunks:
        return None, "chunks is empty"
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            return None, f"chunks[{i}] is not a dict"
        if "turn_id" not in chunk or not isinstance(chunk["turn_id"], str):
            return None, f"chunks[{i}] missing string turn_id"
    return {c["turn_id"] for c in chunks}, None


# Content arrays whose presence makes grounding mandatory on a
# grounded (1.1.0) artifact. If any of these is a non-empty list the
# artifact asserts facts about the transcript, so it must carry a
# non-empty ``grounding`` array — otherwise every extracted item is
# unattributed and a fabricated item promotes silently. This is the
# fail-closed floor the runner's per-item check cannot provide (that
# check passes vacuously when ``grounding`` is an empty list).
GROUNDING_CONTENT_KEYS: tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
    "commitments",
    "risks",
    "cross_references",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
)

GROUNDING_MISSING_FOR_CONTENT = "grounding_missing_for_content"
GROUNDING_COVERAGE_EVAL_TYPE = "grounding_coverage"


def _has_content(artifact: Artifact) -> bool:
    for key in GROUNDING_CONTENT_KEYS:
        value = artifact.payload.get(key)
        if isinstance(value, list) and len(value) > 0:
            return True
    return False


def run_grounding_coverage_eval(artifact: Artifact) -> Artifact:
    """Fail-closed grounding floor for the live-LLM path.

    The runner's 1.1.0 per-item check verifies every ``grounding``
    entry has a non-empty ``source_turns``, but it passes vacuously when
    ``grounding`` itself is ``[]`` or absent. So a model that extracts
    decisions yet emits ``grounding: []`` would promote with zero
    attribution. This eval closes that hole: if the artifact carries any
    content item, ``grounding`` MUST be a non-empty list. Per-entry
    turn-id resolution is still enforced separately by
    :func:`run_source_turn_validity_eval_from_chunks`."""
    grounding = artifact.payload.get("grounding")
    if _has_content(artifact) and not (
        isinstance(grounding, list) and len(grounding) > 0
    ):
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[GROUNDING_MISSING_FOR_CONTENT],
            eval_type=GROUNDING_COVERAGE_EVAL_TYPE,
        )
    return _eval_result(
        artifact,
        passed=True,
        reason_codes=[],
        eval_type=GROUNDING_COVERAGE_EVAL_TYPE,
    )


def run_source_turn_validity_eval(
    artifact: Artifact,
    source_record_path: Path | str | None,
) -> Artifact:
    """Run the source_turn_validity check against the on-disk
    source_record. Always returns an ``eval_result`` artifact; never
    raises on missing or malformed source_record — the eval fails with
    ``source_record_invalid``."""
    record, reason = _load_source_record(source_record_path)
    if record is None:
        # Fail-closed: a missing or malformed source_record is an
        # explicit fail. Document the cause inline so a new engineer
        # can read the eval_result and know what to fix.
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[
                f"{SOURCE_RECORD_INVALID}:{reason}"
            ],
        )

    valid_turn_ids = _build_valid_turn_ids(record)
    reason_codes = _collect_unresolved_reason_codes(artifact, valid_turn_ids)
    return _eval_result(
        artifact, passed=not reason_codes, reason_codes=reason_codes
    )


def run_source_turn_validity_eval_from_chunks(
    artifact: Artifact,
    chunks: object,
) -> Artifact:
    """In-memory twin of :func:`run_source_turn_validity_eval` for the
    live-LLM path, which produces chunks in memory instead of writing a
    data-lake source_record to disk.

    Same validation rule, same ``eval_result`` envelope, same
    ``decide_control`` gate — only the source of the valid turn-id set
    differs. Fail-closed: an absent / malformed chunk list fails the
    eval with ``chunks_invalid`` rather than passing on missing input,
    so a workflow that forgot to pass chunks cannot promote an
    ungrounded artifact."""
    valid_turn_ids, reason = _valid_turn_ids_from_chunks(chunks)
    if valid_turn_ids is None:
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[f"{CHUNKS_INVALID}:{reason}"],
        )
    reason_codes = _collect_unresolved_reason_codes(artifact, valid_turn_ids)
    return _eval_result(
        artifact, passed=not reason_codes, reason_codes=reason_codes
    )
