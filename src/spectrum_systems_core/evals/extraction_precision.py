"""Extraction precision eval (Phase Z.3).

For every extracted item in an artifact payload that carries
``source_turns``, this eval verifies that the claimed source turns
actually contain text supporting the extracted item.

Algorithm (deterministic, in order):

  1. Exact substring match of the extracted text inside ANY of the
     listed source turns → that item passes.
  2. ``difflib.SequenceMatcher`` ratio between the extracted text and
     ANY of the listed source turns >= ``LCS_THRESHOLD`` (0.7) → that
     item passes (paraphrase match).
  3. Neither fires → that item fails the eval with
     ``source_text_not_grounded:<item_id>``.

The eval reads ``source_record`` from disk every time. A missing or
unreadable ``source_record`` is an explicit fail (``source_record_missing``);
it never passes silently. An empty ``source_record_path`` string is
treated identically to a missing record so a wiring regression cannot
slip through.

The eval is deliberately silent on items that have no extracted text
content but carry a non-empty ``source_turns`` list — those would
otherwise be a free pass. The empty-source_turns case is treated as a
fail: an item with text and zero source turns is not grounded.

Item identification (used in finding codes):

  - ``id`` field if present on the item
  - else ``decision-<idx>``, ``action-<idx>``, etc., derived from the
    parent collection name
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from ..artifacts import Artifact, new_artifact

# Eval identifier
EVAL_TYPE = "extraction_precision"

# Paraphrase threshold. 0.7 is intentionally permissive: spectrum
# transcripts contain heavy paraphrasing of formal language. Lowering
# this without consultation is a trust regression — adversarial tests
# pin the 0.7 boundary.
LCS_THRESHOLD = 0.7

# Reason code prefixes.
SOURCE_RECORD_MISSING = "source_record_missing"
SOURCE_TEXT_NOT_GROUNDED_PREFIX = "source_text_not_grounded:"
TURN_ID_NOT_FOUND_PREFIX = "turn_id_not_found:"
EMPTY_SOURCE_TURNS_PREFIX = "empty_source_turns:"

# Payload keys whose list contents may carry ``source_turns`` per item.
# Mirrors the convention used by ``source_turn_validity`` plus the
# product-level lists used by the workflows.
ITEM_LIST_KEYS: tuple[str, ...] = (
    "grounding",
    "items",
    "decisions",
    "action_items",
    "actions",
    "open_questions",
    "questions",
)


def _lcs_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _load_source_record(
    source_record_path: Path | str | None,
) -> tuple[dict | None, str | None]:
    if source_record_path is None or source_record_path == "":
        # The empty-string case is a wiring regression sentinel: the
        # pipeline always passes a real path; an empty string means
        # someone forgot to populate it. Fail rather than skip.
        return None, "source_record_path is empty or None"
    path = Path(source_record_path)
    if not path.is_file():
        return None, f"source_record not found at {path}"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"source_record unreadable: {exc}"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"source_record not valid JSON: {exc}"
    if not isinstance(record, dict):
        return None, "source_record is not a JSON object"
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None, "source_record.payload is not a dict"
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None, "source_record.payload.chunks is missing or empty"
    return record, None


def _build_turn_text_lookup(record: dict) -> dict[str, str]:
    """Map turn_id -> turn text from the source_record."""
    lookup: dict[str, str] = {}
    for chunk in record["payload"]["chunks"]:
        if isinstance(chunk, dict):
            turn_id = chunk.get("turn_id")
            text = chunk.get("text", "")
            if isinstance(turn_id, str) and isinstance(text, str):
                lookup[turn_id] = text
    return lookup


def _item_text(item: dict) -> str:
    """Extract the text content of one item.

    Looks at ``text`` first (the canonical field for decisions /
    actions / questions), then falls back to ``source_excerpt``
    (used by the grounding layer for its evidence spans), then to
    ``summary``. Returns ``""`` when nothing text-like is present.
    """
    for key in ("text", "source_excerpt", "summary"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _item_identifier(parent_key: str, idx: int, item: dict) -> str:
    """Stable id for the finding payload. Pulled from ``id`` if the
    extractor set one, else synthesised from collection + index so
    a new engineer can locate the item in the artifact."""
    if isinstance(item, dict):
        for key in ("id", "item_id", "decision_id", "action_id"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v
    # Singularise the parent_key for readability when possible.
    singular = {
        "decisions": "decision",
        "action_items": "action",
        "actions": "action",
        "open_questions": "question",
        "questions": "question",
        "items": "item",
        "grounding": "grounding",
    }.get(parent_key, parent_key)
    return f"{singular}-{idx}"


def _eval_result(
    target: Artifact, passed: bool, reason_codes: list[str]
) -> Artifact:
    payload = {
        "eval_type": EVAL_TYPE,
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


def _iter_items_with_source_turns(artifact: Artifact):
    """Yield (parent_key, item_index, item) for every dict-shaped item
    under one of the known item-list keys that carries a
    ``source_turns`` field."""
    payload = artifact.payload or {}
    for parent_key in ITEM_LIST_KEYS:
        items = payload.get(parent_key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, dict) and "source_turns" in item:
                yield parent_key, idx, item


def run_extraction_precision_eval(
    artifact: Artifact,
    source_record_path: Path | str | None,
) -> Artifact:
    """Run the extraction precision check.

    Always returns an ``eval_result`` artifact; never raises. A missing
    or malformed source_record fails the eval with
    ``source_record_missing``; it never passes silently."""
    record, reason = _load_source_record(source_record_path)
    if record is None:
        return _eval_result(
            artifact,
            passed=False,
            reason_codes=[f"{SOURCE_RECORD_MISSING}:{reason}"],
        )

    turn_text_lookup = _build_turn_text_lookup(record)
    reason_codes: list[str] = []

    for parent_key, idx, item in _iter_items_with_source_turns(artifact):
        item_id = _item_identifier(parent_key, idx, item)
        item_text = _item_text(item)
        source_turns = item.get("source_turns")

        if not isinstance(source_turns, list):
            # source_turn_validity already covers this case; restating
            # here keeps the eval defensible on its own.
            reason_codes.append(
                f"{SOURCE_TEXT_NOT_GROUNDED_PREFIX}{item_id}"
                f"|source_turns_not_a_list"
            )
            continue

        # An item with text but no source_turns at all is not grounded.
        # Without this rule, an extractor could pass the gate by
        # emitting ``source_turns: []`` on every item.
        if not source_turns and item_text:
            reason_codes.append(
                f"{EMPTY_SOURCE_TURNS_PREFIX}{item_id}"
                f"|item_text={item_text[:80]!r}"
            )
            continue

        # Validate each claimed turn_id exists in the source_record.
        resolved_texts: list[str] = []
        for turn_id in source_turns:
            if not isinstance(turn_id, str) or turn_id not in turn_text_lookup:
                reason_codes.append(
                    f"{TURN_ID_NOT_FOUND_PREFIX}{turn_id}"
                    f"|item={item_id}"
                )
                continue
            resolved_texts.append(turn_text_lookup[turn_id])

        if not resolved_texts:
            # All listed turn_ids were invalid — already accounted for
            # by per-turn turn_id_not_found findings above. Don't
            # double-fail this item.
            continue

        if not item_text:
            # No text to ground. Skip silently — the required-field
            # eval owns the "item must have text" claim.
            continue

        # Try exact substring against any resolved turn first.
        item_text_lower = item_text.lower().strip()
        grounded = any(
            item_text_lower in tt.lower() for tt in resolved_texts
        )
        if not grounded:
            # Fall through to LCS paraphrase match.
            grounded = any(
                _lcs_ratio(item_text, tt) >= LCS_THRESHOLD
                for tt in resolved_texts
            )
        if not grounded:
            reason_codes.append(
                f"{SOURCE_TEXT_NOT_GROUNDED_PREFIX}{item_id}"
                f"|item_text={item_text[:80]!r}"
            )

    return _eval_result(
        artifact, passed=not reason_codes, reason_codes=reason_codes
    )


__all__ = [
    "EVAL_TYPE",
    "LCS_THRESHOLD",
    "SOURCE_RECORD_MISSING",
    "SOURCE_TEXT_NOT_GROUNDED_PREFIX",
    "TURN_ID_NOT_FOUND_PREFIX",
    "EMPTY_SOURCE_TURNS_PREFIX",
    "ITEM_LIST_KEYS",
    "run_extraction_precision_eval",
]
