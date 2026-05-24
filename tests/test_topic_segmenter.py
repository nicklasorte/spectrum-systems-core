"""Phase 5 Variant B — tests for the topic segmenter (G-SCHEMA-SPLIT).

Two contracts:

1. The segmenter assigns EVERY chunk to exactly one segment (no
   unassigned chunks, no double-assignment).
2. The segmenter never writes to the data lake — it produces an
   in-memory ``TopicSegment`` list only.

These are unit tests against the pure-Python segmenter helpers; the
LLM pass is exercised separately at the integration layer.
"""
from __future__ import annotations

import pytest

from spectrum_systems_core.workflows import topic_segmenter
from spectrum_systems_core.workflows.topic_segmenter import (
    ALL_TOPIC_CATEGORIES,
    ALWAYS_EXTRACTED_TYPES,
    TOPIC_CATEGORY_KICKOFF,
    TOPIC_CATEGORY_SCHEDULE,
    TOPIC_CATEGORY_TECHNICAL_ANALYSIS,
    TOPIC_TYPE_MAP,
    TopicSegment,
    assign_chunks_to_segments,
    build_segmenter_prompt,
    parse_segmenter_response,
)


def _make_chunks(n: int) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"t{i:04d}",
            "turn_id": f"t{i:04d}",
            "speaker": f"S{i % 3}",
            "text": f"text-{i}",
        }
        for i in range(1, n + 1)
    ]


def test_segmenter_assigns_all_chunks_to_segments() -> None:
    """Every chunk MUST land in exactly one segment.

    Partial coverage from the Pass-1 model would silently drop content
    from the final extraction. The segmenter raises on the first sign
    of a missed chunk so the bug surfaces at the boundary, not in
    Pass 2.
    """
    chunks = _make_chunks(6)
    assignments = [
        {
            "topic_label": "Opening Remarks",
            "topic_category": TOPIC_CATEGORY_KICKOFF,
            "chunk_ids": ["t0001", "t0002"],
        },
        {
            "topic_label": "Technical Analysis Overview",
            "topic_category": TOPIC_CATEGORY_TECHNICAL_ANALYSIS,
            "chunk_ids": ["t0003", "t0004"],
        },
        {
            "topic_label": "Schedule",
            "topic_category": TOPIC_CATEGORY_SCHEDULE,
            "chunk_ids": ["t0005", "t0006"],
        },
    ]
    segments = assign_chunks_to_segments(chunks, assignments)
    seen_ids = [cid for seg in segments for cid in seg.chunk_ids]
    assert sorted(seen_ids) == [f"t{i:04d}" for i in range(1, 7)]
    assert len(seen_ids) == len(set(seen_ids)), "double-assignment detected"


def test_segmenter_preserves_chronological_order() -> None:
    """Segments MUST be returned in chronological chunk order.

    Even if the model returns segments out of order, the segmenter
    re-sorts by the first chunk's position in the input list. This
    is the deterministic property: same Pass-1 output → same segment
    sequence.
    """
    chunks = _make_chunks(4)
    assignments = [
        # Intentionally reverse order in the Pass-1 output
        {
            "topic_label": "Late",
            "topic_category": TOPIC_CATEGORY_SCHEDULE,
            "chunk_ids": ["t0003", "t0004"],
        },
        {
            "topic_label": "Early",
            "topic_category": TOPIC_CATEGORY_KICKOFF,
            "chunk_ids": ["t0001", "t0002"],
        },
    ]
    segments = assign_chunks_to_segments(chunks, assignments)
    assert [s.topic_label for s in segments] == ["Early", "Late"]
    assert segments[0].start_turn == "t0001"
    assert segments[0].end_turn == "t0002"
    assert segments[1].start_turn == "t0003"
    assert segments[1].end_turn == "t0004"


def test_segmenter_topic_type_map_covers_all_categories() -> None:
    """TOPIC_TYPE_MAP MUST have an entry for every declared category.

    A category in :data:`ALL_TOPIC_CATEGORIES` without a TOPIC_TYPE_MAP
    entry would silently degrade to an extraction with no types
    requested. This pins the symmetry.
    """
    missing = [
        cat for cat in ALL_TOPIC_CATEGORIES if cat not in TOPIC_TYPE_MAP
    ]
    assert not missing, (
        f"TOPIC_TYPE_MAP missing entries for categories: {missing}"
    )
    # Every entry must list at least one type — an empty tuple would
    # silently drop the segment's extraction.
    for cat, types in TOPIC_TYPE_MAP.items():
        assert len(types) >= 1, f"category {cat} has empty TOPIC_TYPE_MAP entry"


def test_filtered_types_includes_always_extracted_descriptive_types() -> None:
    """Every segment's filtered_types MUST include the ALWAYS list.

    Descriptive types (attendees, agenda_item, ...) are structural,
    not topic-bound. Dropping them per-segment would lose the
    meeting's roster on any segment that isn't kickoff-categorized.
    """
    segment = TopicSegment(
        topic_label="Technical Analysis",
        topic_category=TOPIC_CATEGORY_TECHNICAL_ANALYSIS,
        chunk_ids=("t0001",),
        chunks=({"chunk_id": "t0001", "text": "x"},),
        start_turn="t0001",
        end_turn="t0001",
    )
    types = segment.filtered_types()
    for descriptive in ALWAYS_EXTRACTED_TYPES:
        assert descriptive in types, (
            f"filtered_types missing always-extracted type {descriptive!r}"
        )


def test_segmented_extraction_uses_filtered_schema_topics_absent() -> None:
    """No segment category MUST allow 'topics' in its filtered_types.

    The whole premise of Variant B is that ``topics`` is derived from
    segmentation, not re-extracted by the LLM. If a future edit added
    ``topics`` to any TOPIC_TYPE_MAP entry, the F1 comparison would
    silently double-count.
    """
    for cat, types in TOPIC_TYPE_MAP.items():
        assert "topics" not in types, (
            f"category {cat} would re-extract topics; topics must be "
            "derived from segmentation only (Variant B invariant)"
        )


def test_segmenter_rejects_unknown_category() -> None:
    """Unknown topic_category from Pass 1 MUST fail-closed.

    Silently accepting a typo'd category would route the segment
    through a missing TOPIC_TYPE_MAP entry and drop all extraction.
    """
    chunks = _make_chunks(2)
    assignments = [
        {
            "topic_label": "Bogus",
            "topic_category": "this_is_not_a_category",
            "chunk_ids": ["t0001", "t0002"],
        }
    ]
    with pytest.raises(ValueError, match="unknown topic_category"):
        assign_chunks_to_segments(chunks, assignments)


def test_segmenter_rejects_unknown_chunk_id() -> None:
    """A chunk_id Pass 1 invented MUST cause a halt.

    Halts on hallucinated IDs rather than dropping them — the
    extraction layer must never see partial coverage.
    """
    chunks = _make_chunks(2)
    assignments = [
        {
            "topic_label": "Phantom",
            "topic_category": TOPIC_CATEGORY_KICKOFF,
            "chunk_ids": ["t9999"],
        }
    ]
    with pytest.raises(ValueError, match="unknown chunk_id"):
        assign_chunks_to_segments(chunks, assignments)


def test_segmenter_rejects_double_assignment() -> None:
    """A chunk_id appearing in two segments MUST raise."""
    chunks = _make_chunks(2)
    assignments = [
        {
            "topic_label": "A",
            "topic_category": TOPIC_CATEGORY_KICKOFF,
            "chunk_ids": ["t0001", "t0002"],
        },
        {
            "topic_label": "B",
            "topic_category": TOPIC_CATEGORY_SCHEDULE,
            "chunk_ids": ["t0002"],
        },
    ]
    with pytest.raises(ValueError, match="more than one segment"):
        assign_chunks_to_segments(chunks, assignments)


def test_segmenter_rejects_missing_chunk() -> None:
    """If any chunk goes unassigned, the segmenter MUST raise.

    Partial coverage from Pass 1 is silently lost content in Pass 2.
    """
    chunks = _make_chunks(3)
    assignments = [
        {
            "topic_label": "Only Two",
            "topic_category": TOPIC_CATEGORY_KICKOFF,
            "chunk_ids": ["t0001", "t0002"],
        }
    ]
    with pytest.raises(ValueError, match="does not cover every chunk"):
        assign_chunks_to_segments(chunks, assignments)


def test_segmenter_output_not_written_to_data_lake() -> None:
    """The segmenter module MUST NOT import any data-lake writer.

    Variant B's invariant: no variant writes intermediate artifacts to
    the data lake. The TopicSegment is in-memory only. A stray import
    of ``data_lake.writer`` from this module would risk silently
    persisting a segmentation artifact.
    """
    import inspect

    source = inspect.getsource(topic_segmenter)
    # The segmenter shouldn't pull in any writer / artifact / promotion
    # module. Failing here forces a reviewer to confirm the boundary.
    for forbidden in (
        "data_lake.writer",
        "from spectrum_systems_core.data_lake",
        "promotion.promoter",
        "artifacts.new_artifact",
    ):
        assert forbidden not in source, (
            f"topic_segmenter imports {forbidden!r}; Variant B requires "
            "the segmenter to be an in-memory intermediate only"
        )


def test_build_segmenter_prompt_is_terse_and_json_shaped() -> None:
    """The Pass-1 prompt MUST request JSON only and stay short.

    The whole point of the lightweight segmentation pass is to be
    cheap; embedding the full extraction prompt would defeat the
    purpose.
    """
    chunks = _make_chunks(3)
    prompt = build_segmenter_prompt(chunks, meeting_type="kickoff")
    assert "STRICT JSON" in prompt
    assert "topic_label" in prompt and "topic_category" in prompt
    # No accidentally-pasted extraction taxonomy
    assert "issue_registry_entry" not in prompt
    assert "verbatim" not in prompt.lower()
    # Includes chunks
    assert "t0001" in prompt


def test_parse_segmenter_response_round_trips() -> None:
    """parse_segmenter_response MUST accept the documented shape."""
    payload = (
        '{"segments": [{"topic_label": "x", '
        '"topic_category": "kickoff", "chunk_ids": ["t0001"]}]}'
    )
    out = parse_segmenter_response(payload)
    assert out == [
        {
            "topic_label": "x",
            "topic_category": "kickoff",
            "chunk_ids": ["t0001"],
        }
    ]


def test_parse_segmenter_response_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_segmenter_response("not-json")


def test_parse_segmenter_response_rejects_missing_segments_key() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        parse_segmenter_response('{"not_segments": []}')
