"""Verbatim source grounding gate (Phase 4.A — G-GROUND-VERBATIM).

Deterministic substring check: every claim-shaped item must have a
``source_quote`` that is a literal substring of its source chunk
after normalization (whitespace, smart quotes, NFKC). When
``source_chunk_id`` is present the check is scoped to that chunk;
when absent the check falls back to the full transcript text
(weaker — and the result records a warning).

Fail-closed: items without a valid ``source_quote`` are rejected and
land in a separate ``ungrounded_items`` artifact so an operator can
audit each failure without rebuilding the run. Empty quotes are
rejected explicitly BEFORE the substring check — ``"" in chunk``
returns True in Python.

This module is pure: no I/O, no model calls, no time-dependent
state. Two calls on the same input produce identical output.

The canonical 14 claim-shaped types that the gate runs on are
declared in :data:`CLAIM_SHAPED_TYPES` so the producer, the
prompt validator, and the gate cannot drift.

Co-existing module: :mod:`spectrum_systems_core.promotion.gate`
implements the Phase 1 (schema 1.4.0) offset-based gate which
remains in force for legacy artifacts. The two modules differ in:

  * algorithm — Phase 1 verifies a byte-match at a declared offset
    against the normalized transcript; Phase 4.A runs a substring
    check against either the named chunk or the whole transcript.
  * normalization — Phase 1 lowercases + collapses whitespace +
    strips ASCII punctuation; Phase 4.A preserves case and
    punctuation but maps smart quotes to straight quotes and
    applies Unicode NFKC.
  * required fields — Phase 1 requires ``source_quote`` AND
    ``quote_offset_normalized``; Phase 4.A requires only
    ``source_quote`` (with optional ``source_chunk_id``).

A future migration will retire :mod:`promotion.gate` in favour of
this module once every producer emits ``source_chunk_id`` and the
data lake has been re-baselined under schema 1.5.0.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


GROUNDING_GATE_SCHEMA_VERSION: str = "1.5.0"
"""Schema version at which the substring-based grounding gate becomes
binding. Read this constant; do NOT re-introduce string literals."""


MIN_QUOTE_LENGTH: int = 10
"""Minimum number of characters in a normalized ``source_quote``
before it can clear the gate. A 1–9 character quote like
``"yes"`` or ``"decided"`` is not a real verbatim span supporting
a propositional claim — it is the model anchoring on a token."""


MAX_DETAIL_QUOTE_CHARS: int = 200
"""How many characters of a rejected quote to include in the
``ItemFailure.detail`` string. Failure detail may appear in PR
descriptions and workflow step summaries; truncate to avoid
splatting full sensitive quotes there."""


# The 14 claim-shaped types the gate validates. Mirrors the schema
# discriminator. Listed in the canonical order from the task spec so
# downstream code that iterates the set in declaration order produces
# deterministic output.
CLAIM_SHAPED_TYPES: frozenset[str] = frozenset(
    {
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "claims",
        "risks",
        "cross_references",
        "regulatory_references",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
        "procedural_ruling",
    }
)


# Smart-quote → straight-quote mapping. Applied to both sides of the
# comparison so a model emitting smart quotes still grounds against a
# transcript with straight quotes (and vice versa).
_SMART_QUOTE_MAP: dict[str, str] = {
    "‘": "'",  # left single
    "’": "'",  # right single
    "‚": "'",  # single low-9
    "‛": "'",  # single high-reversed-9
    "“": '"',  # left double
    "”": '"',  # right double
    "„": '"',  # double low-9
    "‟": '"',  # double high-reversed-9
    "′": "'",  # prime
    "″": '"',  # double prime
}


class GroundingFailureReason(str, Enum):
    """Why a single item failed the grounding gate."""

    MISSING_SOURCE_QUOTE = "missing_source_quote"
    EMPTY_SOURCE_QUOTE = "empty_source_quote"
    TOO_SHORT = "too_short"
    NOT_SUBSTRING = "not_substring"
    UNKNOWN_CHUNK_ID = "unknown_chunk_id"


@dataclass(frozen=True)
class ItemFailure:
    """One rejected item plus the reason it was rejected."""

    item_index: int
    extraction_type: str
    reason: GroundingFailureReason
    detail: str
    source_quote: str | None = None
    source_chunk_id: str | None = None


@dataclass(frozen=True)
class GroundingResult:
    """Outcome of running the gate on one extraction artifact.

    Attributes:
        passed: True iff every item in every claim-shaped type
            produced a valid ``source_quote``. False if any single
            item failed; the failure list carries the details.
        total_items: number of items the gate examined across all
            claim-shaped types.
        grounded_items: items whose quote was a valid substring.
        ungrounded_items: total_items - grounded_items.
        failures: per-item failure records, in stable iteration
            order: type first (per :data:`CLAIM_SHAPED_TYPES`
            declaration order), then per-item index.
        warnings: free-form messages the operator should see
            (e.g. ``source_chunk_id`` missing — fell back to full
            transcript).
    """

    passed: bool
    total_items: int
    grounded_items: int
    ungrounded_items: int
    failures: tuple[ItemFailure, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def normalize_for_grounding(text: str) -> str:
    """Return ``text`` normalized for substring comparison.

    Operations applied in order:

      1. Map smart quotes (curly single/double, primes) to their
         straight ASCII equivalents. Done BEFORE NFKC because the
         compatibility decomposition of ``U+2033`` (DOUBLE PRIME)
         is ``''`` (two apostrophes), which destroys the
         double-quote signal we want to preserve.
      2. Apply Unicode NFKC normalization (compose combining
         characters; collapse remaining compatibility forms).
      3. Collapse every run of ASCII whitespace (space, tab, CR,
         LF, FF, VT) to a single ``" "``.
      4. Strip leading and trailing whitespace.

    The normalizer is idempotent: applying it twice yields the
    same result as applying it once. Case is preserved — the gate
    treats casing as a real signal so ``DOD`` and ``DoD`` are NOT
    folded together.

    Args:
        text: the input string. May be empty.

    Returns:
        The normalized form. Empty input maps to ``""``.
    """
    if not text:
        return ""
    if any(ch in _SMART_QUOTE_MAP for ch in text):
        text = "".join(_SMART_QUOTE_MAP.get(ch, ch) for ch in text)
    normalized = unicodedata.normalize("NFKC", text)
    # Collapse whitespace runs. ``str.split()`` with no argument splits
    # on ANY run of whitespace (the same set we want to collapse) and
    # discards empty leading/trailing fragments, giving us the strip +
    # collapse in one pass.
    return " ".join(normalized.split())


def _coerce_quote(value: Any) -> str | None:
    """Return ``value`` as a string if it looks like a quote, else None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _validate_one_item(
    item_index: int,
    extraction_type: str,
    item: Mapping[str, Any],
    chunks_by_id: Mapping[str, str],
    full_transcript_normalized: str,
    warnings: list[str],
) -> ItemFailure | None:
    """Validate one item. Return ``None`` on success, a failure on miss.

    The chunks dict is expected pre-normalized for performance; the
    full transcript is also pre-normalized. ``warnings`` is mutated
    in place to record fall-back-to-transcript events.
    """
    raw_quote = item.get("source_quote") if isinstance(item, Mapping) else None
    chunk_id_raw = item.get("source_chunk_id") if isinstance(item, Mapping) else None
    source_chunk_id = chunk_id_raw if isinstance(chunk_id_raw, str) and chunk_id_raw else None

    quote = _coerce_quote(raw_quote)
    if quote is None or "source_quote" not in item:
        return ItemFailure(
            item_index=item_index,
            extraction_type=extraction_type,
            reason=GroundingFailureReason.MISSING_SOURCE_QUOTE,
            detail="item has no source_quote field",
            source_quote=None,
            source_chunk_id=source_chunk_id,
        )

    if quote.strip() == "":
        # Explicit empty-string check BEFORE substring check.
        # ``"" in chunk_text`` returns True in Python — without this
        # branch every empty quote would silently pass.
        return ItemFailure(
            item_index=item_index,
            extraction_type=extraction_type,
            reason=GroundingFailureReason.EMPTY_SOURCE_QUOTE,
            detail="source_quote is empty or whitespace-only",
            source_quote=quote,
            source_chunk_id=source_chunk_id,
        )

    normalized_quote = normalize_for_grounding(quote)
    if len(normalized_quote) < MIN_QUOTE_LENGTH:
        return ItemFailure(
            item_index=item_index,
            extraction_type=extraction_type,
            reason=GroundingFailureReason.TOO_SHORT,
            detail=(
                f"normalized source_quote is {len(normalized_quote)} chars; "
                f"minimum is {MIN_QUOTE_LENGTH}"
            ),
            source_quote=quote[:MAX_DETAIL_QUOTE_CHARS],
            source_chunk_id=source_chunk_id,
        )

    # Determine the haystack: named chunk if known, else full transcript.
    haystack: str
    if source_chunk_id is not None:
        if source_chunk_id not in chunks_by_id:
            return ItemFailure(
                item_index=item_index,
                extraction_type=extraction_type,
                reason=GroundingFailureReason.UNKNOWN_CHUNK_ID,
                detail=(
                    f"source_chunk_id {source_chunk_id!r} is not in the "
                    f"transcript's chunk index"
                ),
                source_quote=quote[:MAX_DETAIL_QUOTE_CHARS],
                source_chunk_id=source_chunk_id,
            )
        haystack = chunks_by_id[source_chunk_id]
    else:
        warnings.append(
            f"item index={item_index} type={extraction_type!r} omitted "
            f"source_chunk_id; gate fell back to full-transcript substring "
            f"check (weaker)"
        )
        haystack = full_transcript_normalized

    if normalized_quote not in haystack:
        return ItemFailure(
            item_index=item_index,
            extraction_type=extraction_type,
            reason=GroundingFailureReason.NOT_SUBSTRING,
            detail=(
                f"normalized source_quote is not a substring of "
                + (
                    f"chunk {source_chunk_id!r}"
                    if source_chunk_id is not None
                    else "the full transcript"
                )
                + f": {normalized_quote[:MAX_DETAIL_QUOTE_CHARS]!r}"
            ),
            source_quote=quote[:MAX_DETAIL_QUOTE_CHARS],
            source_chunk_id=source_chunk_id,
        )
    return None


def check_grounding(
    items_by_type: Mapping[str, list[Mapping[str, Any]] | Any],
    chunks_by_id: Mapping[str, str],
    full_transcript_text: str,
) -> GroundingResult:
    """Run the grounding gate against a bundle of claim-shaped items.

    Args:
        items_by_type: mapping from claim-shaped type name to a list
            of items. Types not in :data:`CLAIM_SHAPED_TYPES` are
            ignored — turn-aggregate and metadata fields are
            grounded by other gates. A list entry that is not a
            Mapping is rejected with ``MISSING_SOURCE_QUOTE``.
        chunks_by_id: mapping from chunk identifier to the raw chunk
            text. The gate normalizes each chunk lazily on first
            use and caches the result for the duration of the call.
        full_transcript_text: the whole transcript as a single
            string. Used as the fallback haystack when an item omits
            ``source_chunk_id``.

    Returns:
        A :class:`GroundingResult` with ``passed`` True iff every
        claim-shaped item produced a valid normalized substring
        match against its chunk (or the full transcript on
        fallback).
    """
    # Pre-normalize the corpus once so the per-item check is cheap.
    normalized_chunks: dict[str, str] = {
        cid: normalize_for_grounding(text) for cid, text in chunks_by_id.items()
    }
    normalized_transcript = normalize_for_grounding(full_transcript_text)

    failures: list[ItemFailure] = []
    warnings: list[str] = []
    total_items = 0

    # Iterate in CLAIM_SHAPED_TYPES declaration order so the failure
    # list is deterministic. ``frozenset`` is unordered; the canonical
    # order is what we wrote in the literal above.
    canonical_order = (
        "decisions",
        "action_items",
        "open_questions",
        "commitments",
        "claims",
        "risks",
        "cross_references",
        "regulatory_references",
        "issue_registry_entry",
        "position_statement",
        "dissent_or_objection",
        "precedent_reference",
        "external_stakeholder_input",
        "procedural_ruling",
    )
    # Sanity assert: the canonical tuple and the frozenset must match.
    assert frozenset(canonical_order) == CLAIM_SHAPED_TYPES, (
        "canonical_order drift — update CLAIM_SHAPED_TYPES and the "
        "canonical_order tuple in lockstep"
    )

    for extraction_type in canonical_order:
        items = items_by_type.get(extraction_type)
        if not items:
            continue
        if not isinstance(items, list):
            # A non-list payload field is itself a grounding violation:
            # we cannot iterate to check items. Surface it as one
            # missing-quote failure so the diagnostic carries forward.
            failures.append(
                ItemFailure(
                    item_index=0,
                    extraction_type=extraction_type,
                    reason=GroundingFailureReason.MISSING_SOURCE_QUOTE,
                    detail=f"{extraction_type} payload is not a list",
                )
            )
            total_items += 1
            continue

        for index, item in enumerate(items):
            total_items += 1
            if not isinstance(item, Mapping):
                failures.append(
                    ItemFailure(
                        item_index=index,
                        extraction_type=extraction_type,
                        reason=GroundingFailureReason.MISSING_SOURCE_QUOTE,
                        detail=(
                            f"{extraction_type}[{index}] is a bare value, "
                            f"cannot carry source_quote"
                        ),
                    )
                )
                continue
            failure = _validate_one_item(
                item_index=index,
                extraction_type=extraction_type,
                item=item,
                chunks_by_id=normalized_chunks,
                full_transcript_normalized=normalized_transcript,
                warnings=warnings,
            )
            if failure is not None:
                failures.append(failure)

    grounded_items = total_items - len(failures)
    return GroundingResult(
        passed=len(failures) == 0,
        total_items=total_items,
        grounded_items=grounded_items,
        ungrounded_items=len(failures),
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def split_grounded_and_ungrounded(
    items_by_type: Mapping[str, list[Mapping[str, Any]] | Any],
    result: GroundingResult,
) -> tuple[dict[str, list[Any]], dict[str, list[dict[str, Any]]]]:
    """Split items into grounded / ungrounded buckets per the result.

    The grounded bucket is a deep-shallow copy of items_by_type with
    only items that passed the gate. The ungrounded bucket carries
    the original item plus the failure metadata (reason, detail).

    Items in non-claim-shaped types pass through unchanged in the
    grounded bucket — the gate does not validate them and they were
    not counted in ``result.total_items``.
    """
    failures_by_index: dict[tuple[str, int], ItemFailure] = {
        (f.extraction_type, f.item_index): f for f in result.failures
    }

    grounded: dict[str, list[Any]] = {}
    ungrounded: dict[str, list[dict[str, Any]]] = {}

    for extraction_type, items in items_by_type.items():
        if extraction_type not in CLAIM_SHAPED_TYPES:
            grounded[extraction_type] = list(items) if isinstance(items, list) else items
            continue
        if not isinstance(items, list):
            continue
        grounded_list: list[Any] = []
        ungrounded_list: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            failure = failures_by_index.get((extraction_type, index))
            if failure is None:
                grounded_list.append(item)
            else:
                ungrounded_list.append(
                    {
                        "item": item,
                        "item_index": failure.item_index,
                        "extraction_type": failure.extraction_type,
                        "reason": failure.reason.value,
                        "detail": failure.detail,
                        "source_chunk_id": failure.source_chunk_id,
                    }
                )
        if grounded_list:
            grounded[extraction_type] = grounded_list
        if ungrounded_list:
            ungrounded[extraction_type] = ungrounded_list

    return grounded, ungrounded


def grounding_gate_result_payload(
    result: GroundingResult,
    *,
    source_id: str,
    run_id: str,
    trace_id: str | None = None,
    extraction_artifact_path: str | None = None,
) -> dict[str, Any]:
    """Build the JSON payload of a ``grounding_gate_result`` artifact.

    Carries the same numerical fields as :class:`GroundingResult`
    plus enough audit metadata that a reviewer can locate the
    extraction artifact the gate ran against.
    """
    drop_rate = (
        result.ungrounded_items / result.total_items if result.total_items else 0.0
    )
    return {
        "artifact_type": "grounding_gate_result",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "run_id": run_id,
        "trace_id": trace_id,
        "extraction_artifact_path": extraction_artifact_path,
        "passed": result.passed,
        "total_items": result.total_items,
        "grounded_count": result.grounded_items,
        "ungrounded_count": result.ungrounded_items,
        "gate_drop_rate": drop_rate,
        "failures": [
            {
                "item_index": f.item_index,
                "extraction_type": f.extraction_type,
                "reason": f.reason.value,
                "detail": f.detail,
                "source_chunk_id": f.source_chunk_id,
            }
            for f in result.failures
        ],
        "warnings": list(result.warnings),
    }


def grounding_gate_bypass_record(
    *,
    source_id: str,
    extraction_artifact_path: str,
    operator: str | None,
    timestamp: str,
    reason: str = "operator override via --disable-grounding-gate",
) -> dict[str, Any]:
    """Build the JSON payload of a ``grounding_gate_bypass_record``.

    Written every time the ``--disable-grounding-gate`` CLI flag is
    set, so every bypass is auditable. The record carries who
    bypassed, when, and the extraction artifact that was promoted
    without grounding verification.
    """
    return {
        "artifact_type": "grounding_gate_bypass_record",
        "schema_version": "1.0.0",
        "source_id": source_id,
        "extraction_artifact_path": extraction_artifact_path,
        "operator": operator or "unknown",
        "timestamp": timestamp,
        "reason": reason,
    }
