"""Phase X2.1 — heuristic agenda boundary detector tests.

These tests defend the trust property that ``agenda_item_id`` is
ALWAYS a non-empty string after the detector runs, even on transcripts
with no detectable headers. The string is either an ``AI-NNN`` id or
the literal ``"unclassified"``.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from spectrum_systems_core.extraction.heuristic_agenda_detector import (
    AGENDA_DETECTION_ENABLED_ENV,
    UNCLASSIFIED_AGENDA_ID,
    AgendaItem,
    agenda_items_to_artifact_list,
    assign_agenda_item_ids,
    detect_agenda_items,
    status_for_detection,
)
from spectrum_systems_core.health.finding import ALL_FINDING_CODES


# --- Headers ---------------------------------------------------------


def test_detects_explicit_agenda_item_prefix() -> None:
    text = (
        "Agenda Item 1: Introductions and Logistics\n"
        "Chair Smith    9:00\n"
        "Welcome everyone.\n"
        "\n"
        "Agenda Item 2: Spectrum Sharing Analysis\n"
        "Engineer Park    9:15\n"
        "Today we will review.\n"
    )
    items = detect_agenda_items(text)
    assert len(items) == 2
    assert items[0].agenda_item_id == "AI-001"
    assert items[1].agenda_item_id == "AI-002"
    assert "Introductions" in items[0].title
    assert "Spectrum" in items[1].title


def test_detects_allcaps_header_with_speaker_lookahead() -> None:
    text = (
        "SPECTRUM SHARING ANALYSIS\n"
        "Engineer Park    9:15\n"
        "Today's first topic.\n"
    )
    items = detect_agenda_items(text)
    assert len(items) == 1
    assert items[0].title.upper() == "SPECTRUM SHARING ANALYSIS"


def test_detects_numbered_format() -> None:
    text = (
        "1. Technical Review of Coordination Criteria\n"
        "Chair Smith    9:00\n"
        "Opening remarks.\n"
        "\n"
        "2) Working Group Status Reports\n"
        "WG Lead Patel    10:00\n"
        "Status update.\n"
    )
    items = detect_agenda_items(text)
    assert len(items) == 2
    assert items[0].title.startswith("Technical")
    assert items[1].title.startswith("Working")


def test_empty_when_no_headers() -> None:
    text = (
        "Chair Smith    9:00\n"
        "Welcome everyone. Today we will discuss the proposal.\n"
        "Engineer Park    9:05\n"
        "Thank you, Chair.\n"
    )
    items = detect_agenda_items(text)
    assert items == []


def test_empty_when_text_empty() -> None:
    assert detect_agenda_items("") == []
    assert detect_agenda_items("   \n   \n") == []


def test_allcaps_without_speaker_lookahead_rejected() -> None:
    # All-caps inside a quoted block with no following speaker turn:
    # do NOT treat as a header.
    text = (
        'Chair Smith    9:00\n'
        '"AS SEEN IN THE PRIOR RECORD" -- I quote, the FCC said so.\n'
    )
    items = detect_agenda_items(text)
    # Either zero items, OR the all-caps fragment was not picked up
    # because there is no following speaker-turn line.
    assert items == []


# --- Chunk assignment ------------------------------------------------


def _chunk(idx: int) -> dict:
    return {"chunk_id": f"c{idx:03d}", "chunk_index": idx, "text": f"t{idx}"}


def test_assign_unclassified_when_no_agenda_items() -> None:
    chunks = [_chunk(i) for i in range(3)]
    annotated = assign_agenda_item_ids(chunks, [])
    assert all(
        c["agenda_item_id"] == UNCLASSIFIED_AGENDA_ID for c in annotated
    )
    # Original chunks must not be mutated.
    assert all("agenda_item_id" not in c for c in chunks)


def test_chunk_in_range_assigned_to_correct_item() -> None:
    items = [
        AgendaItem("AI-001", "First", 0, 1),
        AgendaItem("AI-002", "Second", 2, 4),
    ]
    chunks = [_chunk(i) for i in range(5)]
    annotated = assign_agenda_item_ids(chunks, items)
    assert annotated[0]["agenda_item_id"] == "AI-001"
    assert annotated[1]["agenda_item_id"] == "AI-001"
    assert annotated[2]["agenda_item_id"] == "AI-002"
    assert annotated[4]["agenda_item_id"] == "AI-002"


def test_chunk_in_gap_falls_back_to_previous_item() -> None:
    items = [
        AgendaItem("AI-001", "First", 0, 1),
        AgendaItem("AI-002", "Second", 5, 7),
    ]
    chunks = [_chunk(i) for i in range(8)]
    annotated = assign_agenda_item_ids(chunks, items)
    # Chunks 2-4 are in the gap; they should attach to the previous
    # item (AI-001).
    assert annotated[2]["agenda_item_id"] == "AI-001"
    assert annotated[3]["agenda_item_id"] == "AI-001"
    assert annotated[4]["agenda_item_id"] == "AI-001"
    assert annotated[5]["agenda_item_id"] == "AI-002"


# --- Source-record serialisation -------------------------------------


def test_artifact_list_shape() -> None:
    items = [AgendaItem("AI-001", "Intro", 0, 4)]
    out = agenda_items_to_artifact_list(items)
    assert out == [{
        "agenda_item_id": "AI-001",
        "title": "Intro",
        "start_turn_index": 0,
        "end_turn_index": 4,
    }]


def test_status_for_detection() -> None:
    assert status_for_detection([]) == "unclassified"
    assert status_for_detection(
        [AgendaItem("AI-001", "x", 0, 0)]
    ) == "detected"


# --- Finding-code wiring + slice file --------------------------------


def test_agenda_detection_failed_in_all_finding_codes() -> None:
    assert "agenda_detection_failed" in ALL_FINDING_CODES


def test_metadata_slices_includes_unclassified_predicate() -> None:
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "data-lake" / "store" / "artifacts" / "evals"
        / "metadata_slices.json"
    )
    assert path.is_file(), f"metadata_slices.json missing at {path}"
    doc = json.loads(path.read_text(encoding="utf-8"))
    slice_ids = [s.get("slice_id") for s in doc.get("slices", [])]
    assert "agenda_detection:unclassified" in slice_ids


# --- Rollback path ---------------------------------------------------


def test_detection_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENDA_DETECTION_ENABLED_ENV, "false")
    text = (
        "Agenda Item 1: Introductions\n"
        "Chair Smith    9:00\n"
        "Welcome.\n"
    )
    # Even with a valid header present, env-disabled returns [].
    assert detect_agenda_items(text) == []
