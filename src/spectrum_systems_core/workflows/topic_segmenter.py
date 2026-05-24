"""Phase 5 Variant B — lightweight topic segmentation pass (G-SCHEMA-SPLIT).

Given a list of speaker-turn chunks, this module assigns each chunk to
an agenda-level topic and groups consecutive same-topic chunks into a
:class:`TopicSegment`. The downstream extraction pass then runs with a
type-filtered schema per segment (see :data:`TOPIC_TYPE_MAP`), which is
the core precision improvement.

Two deliberate non-goals:

* No artifact write. ``TopicSegment`` is an in-memory intermediate;
  the data lake never sees segmentation output. Only the final
  per-meeting extraction artifact is written. The constitution's
  "promotion gate applies to JSON artifacts only" rule binds — this
  module produces no JSON artifact at all.
* No model call inside this module. The segmenter has two surfaces:
  the pure :func:`assign_chunks_to_segments` helper that takes a
  ``{chunk_id: category}`` mapping and returns segments (used by tests
  and offline drivers), and the prompt builder
  :func:`build_segmenter_prompt` that returns the string a future
  Variant-B CLI command will send to Haiku for the segmentation pass.

The new CLI command that actually orchestrates Pass 1 + Pass 2 lives
in :mod:`spectrum_systems_core.cli` and is gated behind a separate
entry point so the existing ``meeting-minutes-llm`` pipeline is
byte-identical to before this module landed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

TOPIC_CATEGORY_KICKOFF: Final[str] = "kickoff"
TOPIC_CATEGORY_SYSTEM_VALIDATION: Final[str] = "system_validation"
TOPIC_CATEGORY_COURSE_OF_ACTION: Final[str] = "course_of_action"
TOPIC_CATEGORY_TECHNICAL_ANALYSIS: Final[str] = "technical_analysis"
TOPIC_CATEGORY_ADJACENT_BAND: Final[str] = "adjacent_band"
TOPIC_CATEGORY_SCHEDULE: Final[str] = "schedule"
TOPIC_CATEGORY_ACTION_ITEMS: Final[str] = "action_items"
TOPIC_CATEGORY_WRAP_UP: Final[str] = "wrap_up"


ALL_TOPIC_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        TOPIC_CATEGORY_KICKOFF,
        TOPIC_CATEGORY_SYSTEM_VALIDATION,
        TOPIC_CATEGORY_COURSE_OF_ACTION,
        TOPIC_CATEGORY_TECHNICAL_ANALYSIS,
        TOPIC_CATEGORY_ADJACENT_BAND,
        TOPIC_CATEGORY_SCHEDULE,
        TOPIC_CATEGORY_ACTION_ITEMS,
        TOPIC_CATEGORY_WRAP_UP,
    }
)


# Per-category whitelist of artifact-payload-type keys the downstream
# extraction pass is allowed to extract within a segment of this
# category. The precision win: instead of asking Haiku to extract all
# 22 types from every chunk, the per-segment prompt enumerates only
# 4-6 types relevant to that topic.
#
# Notes on type choices (calibrated against the precision-negatives
# from Phase 4.B):
#   * ``topics`` is deliberately absent from every segment — topics
#     are derived from the segmentation itself, not re-extracted.
#   * ``technical_parameters`` only appears in technical / adjacent
#     band segments to control its biggest false-positive lever.
#   * ``issue_registry_entry`` only in system_validation /
#     course_of_action segments (matches where the corpus actually
#     records issue items).
#   * The descriptive types (attendees, agenda_item, meeting_phases,
#     sentiment_indicators, glossary_definition, named_artifacts)
#     remain extracted across all segments because they are
#     structural — see ALWAYS_EXTRACTED_TYPES below.
TOPIC_TYPE_MAP: Final[dict[str, tuple[str, ...]]] = {
    TOPIC_CATEGORY_KICKOFF: (
        "decisions",
        "action_items",
        "procedural_ruling",
        "attendees",
    ),
    TOPIC_CATEGORY_SYSTEM_VALIDATION: (
        "decisions",
        "action_items",
        "risks",
        "commitments",
        "issue_registry_entry",
    ),
    TOPIC_CATEGORY_COURSE_OF_ACTION: (
        "decisions",
        "open_questions",
        "position_statement",
        "issue_registry_entry",
    ),
    TOPIC_CATEGORY_TECHNICAL_ANALYSIS: (
        "claims",
        "technical_parameters",
        "risks",
        "regulatory_references",
    ),
    TOPIC_CATEGORY_ADJACENT_BAND: (
        "claims",
        "risks",
        "technical_parameters",
        "open_questions",
    ),
    TOPIC_CATEGORY_SCHEDULE: (
        "scheduled_events",
        "action_items",
        "commitments",
    ),
    TOPIC_CATEGORY_ACTION_ITEMS: (
        "action_items",
        "commitments",
        "scheduled_events",
    ),
    TOPIC_CATEGORY_WRAP_UP: (
        "action_items",
        "decisions",
    ),
}


# Types that ALWAYS go through the extraction pass regardless of the
# segment category — these are descriptive/structural, not claim-shaped.
# Listing them here means the per-segment filtered schema doesn't drop
# the meeting's roster, agenda, or named-artifact list.
ALWAYS_EXTRACTED_TYPES: Final[tuple[str, ...]] = (
    "attendees",
    "agenda_item",
    "meeting_phases",
    "sentiment_indicators",
    "glossary_definition",
    "named_artifacts",
)


@dataclass(frozen=True)
class TopicSegment:
    """One contiguous run of chunks belonging to a single agenda topic.

    ``chunk_ids`` and ``chunks`` carry the same chunks ordered the same
    way; the ID-only field exists so a debug dump doesn't have to
    repeat the full chunk text.

    ``start_turn`` / ``end_turn`` are the turn_id strings of the first
    and last chunks for the segment (stable across runs given the same
    input). The dataclass is frozen so a segment is immutable once
    constructed.
    """

    topic_label: str
    topic_category: str
    chunk_ids: tuple[str, ...]
    chunks: tuple[Mapping[str, Any], ...]
    start_turn: str
    end_turn: str

    def filtered_types(self) -> tuple[str, ...]:
        """Return the type tuple the extractor should request for this segment."""
        category_types = TOPIC_TYPE_MAP.get(self.topic_category, ())
        # Stable de-dup: preserve order, drop repeats.
        seen: set[str] = set()
        out: list[str] = []
        for t in (*category_types, *ALWAYS_EXTRACTED_TYPES):
            if t not in seen:
                seen.add(t)
                out.append(t)
        return tuple(out)


def assign_chunks_to_segments(
    chunks: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
) -> tuple[TopicSegment, ...]:
    """Build segments from per-topic chunk lists.

    ``assignments`` is the structure the Pass-1 model returns:
    a list of dicts with ``topic_label``, ``topic_category``,
    and ``chunk_ids``. The function:

    * validates every category is in :data:`ALL_TOPIC_CATEGORIES`;
    * validates every chunk id resolves to exactly one chunk;
    * preserves chronological order (segments are returned sorted by
      the first chunk's index in ``chunks``);
    * raises ``ValueError`` if any chunk is unassigned (segmentation
      must cover the transcript — partial coverage is a Pass-1 bug).

    The deterministic ordering is the trust property: two runs over
    the same Pass-1 output produce the same segment sequence.
    """
    if not chunks:
        return ()

    # Index chunks by id for O(1) lookup. turn_id falls back to id.
    by_id: dict[str, Mapping[str, Any]] = {}
    index_of: dict[str, int] = {}
    for idx, chunk in enumerate(chunks):
        cid = _chunk_id(chunk)
        if not cid:
            raise ValueError(
                f"chunk at index {idx} has no chunk_id/id/turn_id; "
                "the segmenter cannot map it"
            )
        if cid in by_id:
            raise ValueError(
                f"duplicate chunk_id {cid!r} in input chunks; "
                "the segmenter requires unique IDs"
            )
        by_id[cid] = chunk
        index_of[cid] = idx

    used: set[str] = set()
    raw_segments: list[tuple[int, TopicSegment]] = []

    for entry in assignments:
        category = entry.get("topic_category")
        label = entry.get("topic_label", "")
        ids_raw = entry.get("chunk_ids", [])
        if not isinstance(ids_raw, (list, tuple)) or not ids_raw:
            raise ValueError(
                f"segment {label!r} has empty or non-list chunk_ids; "
                "the segmenter must assign at least one chunk per segment"
            )
        if category not in ALL_TOPIC_CATEGORIES:
            raise ValueError(
                f"unknown topic_category {category!r}; expected one of "
                f"{sorted(ALL_TOPIC_CATEGORIES)}"
            )
        ids: list[str] = []
        for cid in ids_raw:
            if not isinstance(cid, str) or cid not in by_id:
                raise ValueError(
                    f"segment {label!r} references unknown chunk_id {cid!r}"
                )
            if cid in used:
                raise ValueError(
                    f"chunk_id {cid!r} assigned to more than one segment"
                )
            used.add(cid)
            ids.append(cid)

        ids.sort(key=lambda c: index_of[c])
        first_idx = index_of[ids[0]]
        segment = TopicSegment(
            topic_label=str(label),
            topic_category=str(category),
            chunk_ids=tuple(ids),
            chunks=tuple(by_id[c] for c in ids),
            start_turn=ids[0],
            end_turn=ids[-1],
        )
        raw_segments.append((first_idx, segment))

    missing = [_chunk_id(c) for c in chunks if _chunk_id(c) not in used]
    if missing:
        raise ValueError(
            "segmentation does not cover every chunk; unassigned: "
            f"{missing[:10]}{'...' if len(missing) > 10 else ''}"
        )

    raw_segments.sort(key=lambda pair: pair[0])
    return tuple(seg for _, seg in raw_segments)


def build_segmenter_prompt(
    chunks: Sequence[Mapping[str, Any]],
    *,
    meeting_type: str = "unknown",
    agenda_items: Sequence[str] = (),
) -> str:
    """Construct the Pass-1 prompt asking Haiku to label topic segments.

    The prompt is deliberately terse and JSON-only. It MUST NOT
    request any of the 22 product types — Pass 1 only labels
    boundaries; Pass 2 does the extraction.
    """
    chunks_payload = [
        {
            "chunk_id": _chunk_id(c),
            "speaker": c.get("speaker") or "",
            "text": c.get("text") or "",
        }
        for c in chunks
    ]
    body = [
        "You are a topic-segmentation assistant. Given speaker-turn "
        "chunks from a spectrum-policy meeting transcript, identify "
        "the major discussion topics. For each topic you list:",
        "- topic_label: a brief descriptive name (5-80 chars)",
        f"- topic_category: one of {sorted(ALL_TOPIC_CATEGORIES)}",
        "- chunk_ids: the chunk IDs belonging to this topic (in order)",
        "",
        "Rules:",
        "- Every chunk_id appears in EXACTLY one segment.",
        "- Segments do not overlap.",
        "- Adjacent same-topic chunks are merged into one segment.",
        f"- Meeting type: {meeting_type}",
    ]
    if agenda_items:
        body.append("- Agenda items (use as candidate boundaries):")
        for item in agenda_items:
            body.append(f"  * {item}")
    body.extend(
        [
            "",
            "Return STRICT JSON only — no prose, no code fences:",
            '{"segments": [{"topic_label": "...", "topic_category": '
            '"...", "chunk_ids": [...]}, ...]}',
            "",
            "Chunks:",
            json.dumps(chunks_payload, sort_keys=True, ensure_ascii=False),
        ]
    )
    return "\n".join(body)


def parse_segmenter_response(response_text: str) -> list[dict[str, Any]]:
    """Parse the Pass-1 model response into the ``assignments`` list.

    Mirrors the conservative parser pattern used elsewhere in the
    workflow: fail loudly on any drift from the documented JSON shape
    rather than guess.
    """
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"segmenter response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            "segmenter response must be a JSON object with a 'segments' key"
        )
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError(
            "segmenter response 'segments' must be a list"
        )
    return list(segments)


def _chunk_id(chunk: Mapping[str, Any]) -> str:
    for key in ("chunk_id", "turn_id", "id"):
        v = chunk.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


__all__ = [
    "ALL_TOPIC_CATEGORIES",
    "ALWAYS_EXTRACTED_TYPES",
    "TOPIC_CATEGORY_ACTION_ITEMS",
    "TOPIC_CATEGORY_ADJACENT_BAND",
    "TOPIC_CATEGORY_COURSE_OF_ACTION",
    "TOPIC_CATEGORY_KICKOFF",
    "TOPIC_CATEGORY_SCHEDULE",
    "TOPIC_CATEGORY_SYSTEM_VALIDATION",
    "TOPIC_CATEGORY_TECHNICAL_ANALYSIS",
    "TOPIC_CATEGORY_WRAP_UP",
    "TOPIC_TYPE_MAP",
    "TopicSegment",
    "assign_chunks_to_segments",
    "build_segmenter_prompt",
    "parse_segmenter_response",
]
