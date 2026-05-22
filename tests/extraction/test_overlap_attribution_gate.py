"""Phase 2.B: tests for the overlap-attribution gate.

The gate rejects items whose ``source_turn_ids`` are ENTIRELY in the
caller-supplied ``overlap_only_turn_ids`` set. Mixed (overlap +
non-overlap) and pure non-overlap items pass. Verbatim items (no
``source_turn_ids``) are untouched.

These tests synthesize chunk lists and artifacts directly so the gate
is exercised without booting the full extraction stack. The gate is a
pure function over its inputs — no I/O, no model calls, no env vars.
"""
from __future__ import annotations

import os

from spectrum_systems_core.data_lake.chunker import chunk_transcript
from spectrum_systems_core.promotion.gate import (
    compute_overlap_only_turn_ids,
    verify_no_overlap_only_attribution,
)


# ----- Gate behaviour -------------------------------------------------------


def _artifact_with_attendees(*items: dict) -> dict:
    """Return a meeting_minutes envelope carrying ``attendees`` items.

    ``attendees`` is in TURN_AGGREGATE_TYPES (gate scope). Other
    turn-aggregate types share the same rejection path so a single
    type exercises every code path.
    """
    return {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {
            "title": "T",
            "summary": "S",
            "attendees": list(items),
        },
    }


def test_rejects_item_whose_source_turn_ids_are_all_overlap_only():
    """Item with every source_turn_id in the overlap set → reject."""
    art = _artifact_with_attendees(
        {"name": "Alice", "source_turn_ids": ["t0001", "t0002"]},
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids={"t0001", "t0002"}
    )
    assert len(report.rejected_items) == 1
    rec = report.rejected_items[0]
    assert rec.reason_code == "failed:extracted_from_overlap_context"
    assert "t0001" in rec.detail and "t0002" in rec.detail
    assert len(report.accepted_items) == 0
    assert report.artifact_blocked is False


def test_passes_item_with_only_non_overlap_source_turn_ids():
    """Item with no overlap turn id → pass."""
    art = _artifact_with_attendees(
        {"name": "Bob", "source_turn_ids": ["t0010"]},
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids={"t0001", "t0002"}
    )
    assert len(report.rejected_items) == 0
    assert len(report.accepted_items) == 1
    assert report.accepted_items[0].grounding_mode == "turn_aggregate"


def test_passes_item_with_mixed_overlap_and_non_overlap():
    """At least one non-overlap source_turn_id → pass (mixed)."""
    art = _artifact_with_attendees(
        {"name": "Carol", "source_turn_ids": ["t0001", "t0010"]},
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids={"t0001", "t0002"}
    )
    assert len(report.rejected_items) == 0
    assert len(report.accepted_items) == 1


def test_empty_overlap_set_passes_every_item():
    """Default-off (CHUNK_OVERLAP_TURNS=0) ⇒ overlap set is empty.

    Every item passes because no source_turn_id is in the empty
    overlap set. This is the gate's byte-identicality invariant for
    pre-Phase-2.B callers.
    """
    art = _artifact_with_attendees(
        {"name": "A", "source_turn_ids": ["t0001"]},
        {"name": "B", "source_turn_ids": ["t0002"]},
        {"name": "C", "source_turn_ids": ["t0003"]},
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids=set()
    )
    assert len(report.rejected_items) == 0
    assert len(report.accepted_items) == 3


def test_item_without_source_turn_ids_is_silent():
    """Missing/empty source_turn_ids is the existing gate's concern."""
    art = _artifact_with_attendees(
        {"name": "NoTurns"},  # no source_turn_ids
        {"name": "Empty", "source_turn_ids": []},
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids={"t0001"}
    )
    # Neither accept nor reject — the gate is silent for items
    # lacking source_turn_ids.
    assert len(report.rejected_items) == 0
    assert len(report.accepted_items) == 0


def test_inverted_specificity_proof():
    """RED TEAM PASS 2: passing a non-overlap turn id MUST NOT reject.

    Inversion of the primary reject test. Proves the test is checking
    the actual rejection logic, not just a structural assertion.
    """
    art = _artifact_with_attendees(
        {"name": "X", "source_turn_ids": ["t0099"]},  # outside the set
    )
    report = verify_no_overlap_only_attribution(
        art, overlap_only_turn_ids={"t0001", "t0002"}
    )
    assert len(report.rejected_items) == 0, (
        "non-overlap turn id should NOT trigger overlap-only rejection"
    )
    assert len(report.accepted_items) == 1


# ----- compute_overlap_only_turn_ids helper --------------------------------


def test_compute_overlap_only_turn_ids_default_chunker_empty():
    """The default Phase-2.B chunker design returns the empty set.

    The chunker prepends overlap text into the recipient chunk; it does
    NOT emit a separate ``overlap: True`` chunk for the source. So
    every turn_id seen by the helper has ``overlap`` absent (False).
    """
    os.environ.pop("CHUNK_OVERLAP_TURNS", None)
    chunks = chunk_transcript(
        "ALICE: hello\nBOB: world\nCAROL: today\n"
    )
    assert compute_overlap_only_turn_ids(chunks) == frozenset()


def test_compute_overlap_only_turn_ids_default_under_overlap_2_empty():
    """Even under CHUNK_OVERLAP_TURNS=2 the chunker still returns empty.

    Because the current chunker design folds overlap into the
    recipient chunk's text rather than emitting separate
    ``overlap: True`` entries, the helper sees no chunk with
    ``overlap: True`` and returns the empty set. The gate is then a
    no-op in production — preserving byte-identicality. The set is
    non-empty only when a future chunker design DOES emit dedicated
    overlap entries; the gate is the place that future design will
    plug in without further changes.
    """
    os.environ["CHUNK_OVERLAP_TURNS"] = "2"
    try:
        chunks = chunk_transcript(
            "ALICE: hello\nBOB: world\nCAROL: today\n"
        )
    finally:
        os.environ.pop("CHUNK_OVERLAP_TURNS", None)
    assert compute_overlap_only_turn_ids(chunks) == frozenset()


def test_compute_overlap_only_turn_ids_synthetic_overlap_entries():
    """Synthetic chunks with ``overlap: True`` produce a non-empty set."""
    # Two entries for t0001: one native (overlap=False), one overlap
    # context copy (overlap=True). Because the native exists, t0001 is
    # NOT overlap-only. t0099 appears only as an overlap copy → IS
    # overlap-only.
    chunks = [
        {"turn_id": "t0001", "overlap": False, "text": "..."},
        {"turn_id": "t0001", "overlap": True, "text": "..."},
        {"turn_id": "t0099", "overlap": True, "text": "..."},
        {"turn_id": "t0100", "overlap": False, "text": "..."},
    ]
    result = compute_overlap_only_turn_ids(chunks)
    assert result == frozenset({"t0099"})


def test_compute_overlap_only_turn_ids_tolerates_chunk_id_alias():
    """Cascade chunks use ``chunk_id`` (UUID) not ``turn_id``.

    The helper reads either field so the gate works for both
    chunkers. ``chunk_metadata_gate`` already treats the two as
    aliases at the gate boundary.
    """
    chunks = [
        {"chunk_id": "uuid-a", "overlap": True, "text": "x"},
        {"chunk_id": "uuid-b", "overlap": False, "text": "y"},
    ]
    result = compute_overlap_only_turn_ids(chunks)
    assert result == frozenset({"uuid-a"})


def test_compute_overlap_only_turn_ids_ignores_malformed_entries():
    """Non-mapping entries / missing IDs do not raise."""
    chunks = [
        "not-a-dict",
        {"turn_id": None, "overlap": True},
        {"turn_id": "", "overlap": True},
        {"overlap": True},  # no id
        {"turn_id": "tA", "overlap": True},
    ]
    result = compute_overlap_only_turn_ids(chunks)
    assert result == frozenset({"tA"})
