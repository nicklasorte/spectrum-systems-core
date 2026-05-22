"""Verbatim span grounding gate (Phase 1, schema_version 1.4.0).

Every item in a 1.4.0 ``meeting_minutes`` artifact MUST be either:

- ``grounding_mode == "verbatim"``: carries a ``source_quote`` that
  appears byte-for-byte in the normalized transcript at the offset
  declared by ``quote_offset_normalized``; or
- ``grounding_mode == "turn_aggregate"``: carries a non-empty
  ``source_turn_ids`` list, every id of which exists in the
  transcript's turn index.

:func:`verify_grounding` returns a :class:`GroundingReport` listing
accepted and rejected items. The caller is responsible for writing
the reject diagnostic and for promoting an artifact that contains
only the accepted items.

If the per-artifact ``grounding_rate`` (accepted / total) is below
:data:`GROUNDING_RATE_FLOOR`, the WHOLE artifact is rejected with
reason code ``grounding_rate_below_floor``: the prompt is so bad
the model is mostly hallucinating; refuse to promote.

This module never calls a model and never reads network resources.
It is pure logic over a transcript and an artifact payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Iterable, Mapping

from ..grounding.normalize import (
    NormalizedTranscript,
    normalize_quote,
    normalize_transcript,
)


GROUNDING_RATE_FLOOR: float = 0.60
"""Minimum per-artifact grounding rate. Below this the artifact is blocked."""


# Item-type → grounding_mode. Mirrors the schema. The gate uses this
# table to dispatch per-item verification and to detect bare
# ``payload[key]`` arrays that the schema knows about. The table is
# the single source of truth in code; the schema's ``grounding_mode``
# discriminator is the source of truth on disk. Tests assert the two
# tables match.
VERBATIM_TYPES: frozenset[str] = frozenset(
    {
        "decisions",
        "action_items",
        "commitments",
        "claims",
        "risks",
        "position_statement",
        "procedural_ruling",
        # The schema key is ``dissent_or_objection`` — it is the schema
        # name (the source of truth per the data lake contract). Some
        # Phase 1 docs refer to this as ``dissents``; the SCHEMA name
        # is canonical so the gate routes against it.
        "dissent_or_objection",
        "external_stakeholder_input",
        "precedent_reference",
        # Schema name is the plural ``regulatory_references``.
        "regulatory_references",
        "technical_parameters",
        "issue_registry_entry",
        "glossary_definition",
        "sentiment_indicators",
    }
)

TURN_AGGREGATE_TYPES: frozenset[str] = frozenset(
    {
        "attendees",
        "topics",
        "agenda_item",
        "meeting_phases",
        "cross_references",
        "named_artifacts",
        "scheduled_events",
        "open_questions",
    }
)


# Stage 2 source-quote-grounding extension (opt-in).
#
# A precision-improving threshold: a verbatim item whose ``source_quote``
# is shorter than the per-type minimum is rejected with
# ``grounding_source_quote_too_short``. The threshold catches the foot-gun
# where a model anchors a 100-word "decision" on a 3-character span like
# "yes" or "decided" — the byte-match passes but the quote is not a real
# verbatim span supporting the claim.
#
# Thresholds are tiered by item-type semantics:
#
# - SUBSTANTIVE (30 chars): the item carries a propositional claim and
#   the supporting span MUST be long enough to actually contain the
#   claim. A 30-char minimum is roughly a short subject + verb + object.
# - SHORT (10 chars): the item is a short verbatim record (a parameter
#   value, a regulatory citation, a glossary term). 10 chars is a lower
#   bound that still rules out 1-3 char fragments.
#
# This threshold is OPT-IN: callers must pass ``min_quote_chars_by_type``
# explicitly to :func:`verify_grounding`. The default behaviour is
# byte-identical to pre-Stage-2 — no behaviour change for existing
# 1.4.0 producers or the comparison engine until the operator opts in.
MIN_QUOTE_CHARS_SUBSTANTIVE: int = 30
"""Minimum source_quote length for substantive verbatim item types."""

MIN_QUOTE_CHARS_SHORT: int = 10
"""Minimum source_quote length for short verbatim item types."""

DEFAULT_MIN_QUOTE_CHARS_BY_TYPE: dict[str, int] = {
    # Substantive types: a claim, a commitment, an objection, a
    # ruling — the supporting span must contain enough text to be the
    # claim, not a token affirming it.
    "decisions": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "action_items": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "commitments": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "claims": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "risks": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "position_statement": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "dissent_or_objection": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "procedural_ruling": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "precedent_reference": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "external_stakeholder_input": MIN_QUOTE_CHARS_SUBSTANTIVE,
    "issue_registry_entry": MIN_QUOTE_CHARS_SUBSTANTIVE,
    # Short types: a parameter value, a citation, a glossary term, a
    # sentiment marker. Substantively shorter content; threshold still
    # rules out 1-3 char fragments.
    "regulatory_references": MIN_QUOTE_CHARS_SHORT,
    "technical_parameters": MIN_QUOTE_CHARS_SHORT,
    "sentiment_indicators": MIN_QUOTE_CHARS_SHORT,
    "glossary_definition": MIN_QUOTE_CHARS_SHORT,
}
"""Per-type minimum source_quote length.

Every entry in :data:`VERBATIM_TYPES` MUST appear here so a Stage 2
caller can never silently bypass the gate by passing a type whose
threshold defaults to zero. The
:func:`test_default_min_quote_chars_table_covers_all_verbatim_types`
test pins this invariant.
"""


@dataclass(frozen=True)
class RejectionRecord:
    """One rejected item with its reason code and audit detail."""

    item_type: str
    item: Any
    reason_code: str
    detail: str
    expected_quote_normalized: str | None = None
    actual_at_offset_normalized: str | None = None
    offset_checked: int | None = None


@dataclass(frozen=True)
class AcceptanceRecord:
    """One accepted item plus the offsets the gate computed for it."""

    item_type: str
    item: Any
    grounding_mode: str
    quote_offset_normalized: int | None = None
    quote_offset_original: int | None = None
    normalized_match_hash: str | None = None


@dataclass(frozen=True)
class GroundingReport:
    """Result of :func:`verify_grounding`.

    Attributes:
        accepted_items: per-item acceptance records.
        rejected_items: per-item rejection records (with reason codes).
        grounding_rate: ``len(accepted) / (len(accepted) + len(rejected))``.
            ``1.0`` when the artifact has no verifiable items at all
            (vacuous pass — there is nothing to ground).
        artifact_blocked: True when the whole artifact must be blocked
            (e.g. ``grounding_rate < GROUNDING_RATE_FLOOR``, or the
            transcript is missing). When True, ``block_reason_code``
            carries the reason and the caller MUST NOT promote.
        block_reason_code: set only when ``artifact_blocked`` is True.
    """

    accepted_items: tuple[AcceptanceRecord, ...] = field(default_factory=tuple)
    rejected_items: tuple[RejectionRecord, ...] = field(default_factory=tuple)
    grounding_rate: float = 1.0
    artifact_blocked: bool = False
    block_reason_code: str | None = None

    def accepted_payload_keys(self) -> dict[str, list[Any]]:
        """Group accepted items back into ``{item_type: [items...]}``.

        The promoter writes the resulting dict into the promoted
        artifact's payload so only ground-truth items survive.
        """
        out: dict[str, list[Any]] = {}
        for rec in self.accepted_items:
            out.setdefault(rec.item_type, []).append(rec.item)
        return out


def _build_turn_id_index(transcript_turn_ids: Iterable[str] | None) -> frozenset[str]:
    if transcript_turn_ids is None:
        return frozenset()
    return frozenset(str(t) for t in transcript_turn_ids)


def _normalized_to_original_offset(
    nt: NormalizedTranscript, normalized_offset: int
) -> int | None:
    """Map a normalized-transcript byte offset back to the original.

    Returns ``None`` if the offset is out of range. An offset equal to
    ``len(nt.text)`` is permitted and maps to ``len(original)`` so a
    quote that ends exactly at end-of-transcript still produces a
    valid original-offset pair.
    """
    if normalized_offset < 0:
        return None
    if normalized_offset < len(nt.position_map):
        return nt.position_map[normalized_offset]
    if normalized_offset == len(nt.text):
        # End-of-text: there is no character to point at; use the
        # last mapped character's offset + 1, or 0 for an empty map.
        if nt.position_map:
            return nt.position_map[-1] + 1
        return 0
    return None


def _verify_verbatim_item(
    item_type: str,
    item: Mapping[str, Any],
    nt: NormalizedTranscript,
    *,
    min_quote_chars: int | None = None,
) -> RejectionRecord | AcceptanceRecord:
    """Verify one verbatim-mode item against the normalized transcript.

    Args:
        item_type: the payload key the item came from.
        item: the per-item dict.
        nt: the normalized transcript.
        min_quote_chars: Stage 2 opt-in. When set, the normalized quote
            must be at least this many characters or the item is
            rejected with ``grounding_source_quote_too_short``. When
            ``None`` (default) the threshold check is skipped — the
            pre-Stage-2 behaviour is preserved byte-for-byte.
    """
    if not isinstance(item, Mapping):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail=(
                "verbatim-mode item is not an object — cannot read "
                "source_quote / quote_offset_normalized"
            ),
        )
    if "source_quote" not in item or item.get("source_quote") in (None, ""):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="missing or empty source_quote",
        )
    if (
        "quote_offset_normalized" not in item
        or item.get("quote_offset_normalized") is None
    ):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="missing quote_offset_normalized",
        )
    source_quote = item["source_quote"]
    offset = item["quote_offset_normalized"]
    if not isinstance(source_quote, str):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="source_quote is not a string",
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="quote_offset_normalized must be a non-negative integer",
        )

    quote_norm = normalize_quote(source_quote)
    if not quote_norm:
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="source_quote normalizes to an empty string",
        )

    # Stage 2 opt-in: per-type minimum-length threshold. Runs BEFORE the
    # byte-match so a too-short quote is rejected as "too short" rather
    # than as "exact_text_not_in_transcript" — the reason code surfaces
    # the actual precision problem (model anchored on a token, not a
    # span). The pre-Stage-2 path (``min_quote_chars is None``) skips
    # this check entirely.
    if min_quote_chars is not None and len(quote_norm) < min_quote_chars:
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_source_quote_too_short",
            detail=(
                f"normalized source_quote is {len(quote_norm)} chars, "
                f"below the per-type minimum of {min_quote_chars} for "
                f"{item_type!r}"
            ),
            expected_quote_normalized=quote_norm,
        )

    # First check the byte-match at the declared offset.
    end = offset + len(quote_norm)
    window = nt.text[offset:end] if 0 <= offset <= len(nt.text) else ""
    if window == quote_norm:
        original_offset = _normalized_to_original_offset(nt, offset)
        if original_offset is None:
            return RejectionRecord(
                item_type=item_type,
                item=item,
                reason_code="grounding_offset_mismatch",
                detail=(
                    "quote matched at normalized offset but original offset "
                    "could not be recovered"
                ),
                expected_quote_normalized=quote_norm,
                actual_at_offset_normalized=window,
                offset_checked=offset,
            )
        return AcceptanceRecord(
            item_type=item_type,
            item=item,
            grounding_mode="verbatim",
            quote_offset_normalized=offset,
            quote_offset_original=original_offset,
            normalized_match_hash=sha256(
                quote_norm.encode("utf-8")
            ).hexdigest(),
        )

    # Byte-mismatch at declared offset. Distinguish "exact text not in
    # transcript at all" (likely paraphrase / hallucination) from a
    # mere offset slip (text is present, offset is wrong).
    if quote_norm in nt.text:
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_offset_mismatch",
            detail=(
                "source_quote is present in the transcript but not at the "
                "declared offset"
            ),
            expected_quote_normalized=quote_norm,
            actual_at_offset_normalized=window,
            offset_checked=offset,
        )
    return RejectionRecord(
        item_type=item_type,
        item=item,
        reason_code="grounding_exact_text_not_in_transcript",
        detail=(
            "source_quote does not appear anywhere in the normalized "
            "transcript — likely paraphrase or fabrication"
        ),
        expected_quote_normalized=quote_norm,
        actual_at_offset_normalized=window,
        offset_checked=offset,
    )


def _verify_turn_aggregate_item(
    item_type: str,
    item: Mapping[str, Any],
    turn_id_index: frozenset[str],
) -> RejectionRecord | AcceptanceRecord:
    if not isinstance(item, Mapping):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="turn_aggregate-mode item is not an object",
        )
    raw = item.get("source_turn_ids")
    if not raw or not isinstance(raw, list):
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_missing_field",
            detail="missing or empty source_turn_ids",
        )
    unknown = [
        str(t) for t in raw if str(t) not in turn_id_index
    ]
    if unknown:
        return RejectionRecord(
            item_type=item_type,
            item=item,
            reason_code="grounding_unknown_turn_id",
            detail=(
                f"unknown turn_id(s): {unknown!r} — not in transcript turn "
                f"index ({len(turn_id_index)} known)"
            ),
        )
    return AcceptanceRecord(
        item_type=item_type,
        item=item,
        grounding_mode="turn_aggregate",
    )


def verify_grounding(
    artifact: Mapping[str, Any],
    transcript: str | None,
    *,
    transcript_turn_ids: Iterable[str] | None = None,
    normalized_transcript: NormalizedTranscript | None = None,
    min_quote_chars_by_type: Mapping[str, int] | None = None,
) -> GroundingReport:
    """Verify every item in ``artifact.payload`` against the transcript.

    Args:
        artifact: a meeting_minutes artifact envelope OR a bare payload
            dict. If the envelope is passed, the payload is read from
            ``artifact["payload"]``; otherwise the dict itself is the
            payload.
        transcript: the raw transcript text. If ``None`` or empty the
            gate fails closed with ``transcript_unreadable``.
        transcript_turn_ids: iterable of known turn ids for
            ``turn_aggregate`` verification. ``None`` is treated as
            "no turn index known" — every ``turn_aggregate`` item will
            be rejected with ``grounding_unknown_turn_id``.
        normalized_transcript: pre-computed normalization. Optional
            performance hint; the gate normalizes on demand if absent.
        min_quote_chars_by_type: Stage 2 opt-in. When supplied, every
            verbatim item whose normalized ``source_quote`` is shorter
            than ``min_quote_chars_by_type[item_type]`` is rejected
            with ``grounding_source_quote_too_short``. An item-type
            absent from the mapping is NOT threshold-checked (a
            missing entry is treated as "no threshold for this type").
            When ``None`` (default), the threshold check is skipped
            for every item — the pre-Stage-2 gate behaviour is
            preserved byte-for-byte. Pass
            :data:`DEFAULT_MIN_QUOTE_CHARS_BY_TYPE` to apply the
            roadmap's recommended thresholds.

    Returns:
        A :class:`GroundingReport`.

    Fail-closed:
        - ``transcript is None`` or ``transcript == ""`` →
          ``artifact_blocked=True``, ``block_reason_code="transcript_unreadable"``.
        - ``grounding_rate < GROUNDING_RATE_FLOOR`` →
          ``artifact_blocked=True``, ``block_reason_code="grounding_rate_below_floor"``.
    """
    if transcript is None or transcript == "":
        return GroundingReport(
            grounding_rate=0.0,
            artifact_blocked=True,
            block_reason_code="transcript_unreadable",
        )

    nt = normalized_transcript or normalize_transcript(transcript)
    turn_index = _build_turn_id_index(transcript_turn_ids)

    payload = artifact.get("payload", artifact) if isinstance(artifact, Mapping) else {}
    accepted: list[AcceptanceRecord] = []
    rejected: list[RejectionRecord] = []

    for item_type in VERBATIM_TYPES:
        items = payload.get(item_type)
        if not items:
            continue
        if not isinstance(items, list):
            # A non-list payload field is itself a grounding violation:
            # we cannot iterate items to verify them. Reject the whole
            # field as a single missing-field record so the diagnostic
            # surfaces the problem.
            rejected.append(
                RejectionRecord(
                    item_type=item_type,
                    item=items,
                    reason_code="grounding_missing_field",
                    detail=f"{item_type} is not a list",
                )
            )
            continue
        for item in items:
            # Legacy string items (plain decisions, action_items) have
            # no place to carry a source_quote; they fail closed.
            if not isinstance(item, Mapping):
                rejected.append(
                    RejectionRecord(
                        item_type=item_type,
                        item=item,
                        reason_code="grounding_missing_field",
                        detail=(
                            f"{item_type} item is a bare value, cannot "
                            "carry source_quote / quote_offset_normalized"
                        ),
                    )
                )
                continue
            min_chars = (
                min_quote_chars_by_type.get(item_type)
                if min_quote_chars_by_type is not None
                else None
            )
            result = _verify_verbatim_item(
                item_type, item, nt, min_quote_chars=min_chars
            )
            if isinstance(result, AcceptanceRecord):
                accepted.append(result)
            else:
                rejected.append(result)

    for item_type in TURN_AGGREGATE_TYPES:
        items = payload.get(item_type)
        if not items:
            continue
        if not isinstance(items, list):
            rejected.append(
                RejectionRecord(
                    item_type=item_type,
                    item=items,
                    reason_code="grounding_missing_field",
                    detail=f"{item_type} is not a list",
                )
            )
            continue
        for item in items:
            if not isinstance(item, Mapping):
                rejected.append(
                    RejectionRecord(
                        item_type=item_type,
                        item=item,
                        reason_code="grounding_missing_field",
                        detail=(
                            f"{item_type} item is a bare value, cannot "
                            "carry source_turn_ids"
                        ),
                    )
                )
                continue
            result = _verify_turn_aggregate_item(
                item_type, item, turn_index
            )
            if isinstance(result, AcceptanceRecord):
                accepted.append(result)
            else:
                rejected.append(result)

    total = len(accepted) + len(rejected)
    rate = 1.0 if total == 0 else len(accepted) / total
    blocked = rate < GROUNDING_RATE_FLOOR
    block_reason = "grounding_rate_below_floor" if blocked else None
    return GroundingReport(
        accepted_items=tuple(accepted),
        rejected_items=tuple(rejected),
        grounding_rate=rate,
        artifact_blocked=blocked,
        block_reason_code=block_reason,
    )


def grounding_rejection_report_payload(
    report: GroundingReport,
    *,
    artifact_id: str,
    artifact_type: str,
    trace_id: str,
    source_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the payload of a ``grounding_rejection_report`` artifact.

    Includes enough information that a new engineer can explain each
    rejection without reading the code: the item, the reason code, the
    expected vs. actual quote at the checked offset, and the offset
    itself.
    """
    return {
        "schema_version": "1.0.0",
        "artifact_type": "grounding_rejection_report",
        "target_artifact_id": artifact_id,
        "target_artifact_type": artifact_type,
        "trace_id": trace_id,
        "source_id": source_id,
        "run_id": run_id,
        "grounding_rate": report.grounding_rate,
        "accepted_count": len(report.accepted_items),
        "rejected_count": len(report.rejected_items),
        "artifact_blocked": report.artifact_blocked,
        "block_reason_code": report.block_reason_code,
        "rejected_items": [
            {
                "item_type": r.item_type,
                "item": _serializable(r.item),
                "reason_code": r.reason_code,
                "detail": r.detail,
                "expected_quote_normalized": r.expected_quote_normalized,
                "actual_at_offset_normalized": r.actual_at_offset_normalized,
                "offset_checked": r.offset_checked,
            }
            for r in report.rejected_items
        ],
    }


def _serializable(value: Any) -> Any:
    """Best-effort to make any payload item JSON-serializable.

    Most items are already plain dicts; we just defensively convert
    Mapping → dict so frozen-mapping helpers do not leak through.
    """
    if isinstance(value, Mapping):
        return {k: _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
