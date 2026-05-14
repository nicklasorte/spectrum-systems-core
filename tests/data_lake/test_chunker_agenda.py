"""Tests for Phase Z.4 agenda-item metadata on chunker output."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from spectrum_systems_core.data_lake import run_transcript_pipeline
from spectrum_systems_core.data_lake.chunker import (
    _detect_agenda_item,
    chunk_transcript,
)


# ---- detector unit cases ------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Item 1: 3.5 GHz band sharing.", "item-1"),
    ("Agenda item 2: Coordination updates.", "item-2"),
    ("ITEM 7.", "item-7"),
    ("1. The new agenda topic", "item-unknown"),
    ("Topic: spectrum allocation review", "item-unknown"),
    ("Agenda: 3.5 GHz", "item-unknown"),
    ("Just some normal turn text.", None),
    ("", None),
    ("   ", None),
])
def test_detect_agenda_item(text, expected):
    assert _detect_agenda_item(text) == expected


# ---- chunk-level emission ----------------------------------------------


def test_chunker_emits_agenda_item_id_on_marker_turn():
    transcript = (
        "CHAIR: Item 1: 3.5 GHz band sharing.\n"
        "ALICE: We have several options.\n"
    )
    chunks = chunk_transcript(transcript)
    assert chunks[0]["agenda_item_id"] == "item-1"


def test_chunker_propagates_agenda_id_until_new_marker():
    transcript = (
        "CHAIR: I call the meeting to order.\n"
        "CHAIR: Item 1: first topic.\n"
        "ALICE: Discussing the first topic in detail.\n"
        "BOB: Still on the first topic here.\n"
        "CHAIR: Item 2: second topic.\n"
        "ALICE: Onto the second topic now.\n"
    )
    chunks = chunk_transcript(transcript)
    ids = [c["agenda_item_id"] for c in chunks]
    # First chunk preceded the first marker → None.
    assert ids[0] is None
    # Marker fires on t0001 and propagates to t0002 + t0003.
    assert ids[1] == "item-1"
    assert ids[2] == "item-1"
    assert ids[3] == "item-1"
    # New marker on t0004 propagates to t0005.
    assert ids[4] == "item-2"
    assert ids[5] == "item-2"


def test_chunker_propagates_across_multiple_subsequent_turns():
    """Pin the propagation semantics explicitly: a turn that has no
    marker of its own inherits the most recent agenda_item_id from
    earlier in the conversation, even several turns away."""
    transcript = (
        "CHAIR: Item 5: long discussion.\n"
        "ALICE: Comment one.\n"
        "BOB: Comment two.\n"
        "ALICE: Comment three.\n"
        "BOB: Comment four.\n"
        "CHAIR: Comment five from the chair.\n"
    )
    chunks = chunk_transcript(transcript)
    # Every chunk after the marker carries item-5.
    for c in chunks:
        assert c["agenda_item_id"] == "item-5"


def test_chunker_emits_null_when_no_agenda_markers():
    transcript = (
        "ALICE: Welcome to the meeting.\n"
        "BOB: Glad to be here.\n"
        "ALICE: Let us talk about coordination.\n"
    )
    chunks = chunk_transcript(transcript)
    for c in chunks:
        assert c["agenda_item_id"] is None


def test_chunker_emits_item_unknown_for_topic_marker():
    transcript = "CHAIR: Topic: 3.5 GHz allocation review.\n"
    chunks = chunk_transcript(transcript)
    assert chunks[0]["agenda_item_id"] == "item-unknown"


# ---- backward compatibility --------------------------------------------


def test_empty_transcript_still_returns_empty_chunk_list():
    assert chunk_transcript("") == []
    assert chunk_transcript("   \n  \n") == []


def test_existing_chunks_still_carry_legacy_fields():
    """Adding agenda_item_id must not strip any pre-existing field —
    line_start, line_end, speaker, text, turn_id all remain."""
    chunks = chunk_transcript("CHAIR: hello there.\n")
    assert set(chunks[0].keys()) >= {
        "turn_id", "speaker", "text", "line_start",
        "line_end", "agenda_item_id",
    }
    assert chunks[0]["turn_id"] == "t0000"


# ---- integration: agenda metadata does not break source_turn_validity --


def test_agenda_metadata_does_not_break_pipeline_or_source_turn_validity(
    tmp_path,
):
    """Phase Y's source_turn_validity must remain green on transcripts
    that now carry agenda_item_id. This is the canonical regression
    guard for the agenda metadata add."""
    src = (
        Path(__file__).parent.parent / "fixtures"
        / "phase_y" / "m-y-meeting-minutes"
    )
    dst = tmp_path / "raw" / "meetings" / "m-y-meeting-minutes"
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="m-y-meeting-minutes",
        workflow_name="meeting_minutes",
    )
    eval_status = {
        r.payload["eval_type"]: r.payload["status"]
        for r in result.eval_results
    }
    assert eval_status.get("source_turn_validity") == "pass", (
        f"source_turn_validity unexpectedly failed: "
        f"{result.eval_results}"
    )
    assert result.promoted is True


def test_agenda_metadata_persists_into_source_record(tmp_path):
    src = (
        Path(__file__).parent.parent / "fixtures"
        / "golden_meetings" / "gm-001-spectrum-planning"
    )
    dst = tmp_path / "raw" / "meetings" / "gm-001-spectrum-planning"
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        meeting_id="gm-001-spectrum-planning",
        workflow_name="meeting_minutes",
    )
    sr_path = Path(result.source_record_path)
    record = json.loads(sr_path.read_text(encoding="utf-8"))
    chunks = record["payload"]["chunks"]
    # Every chunk has the field, and the propagation in gm-001 means
    # turns after t0001 carry item-1 / item-2.
    assert all("agenda_item_id" in c for c in chunks)
    by_turn = {c["turn_id"]: c["agenda_item_id"] for c in chunks}
    assert by_turn["t0001"] == "item-1"
    assert by_turn["t0002"] == "item-1"
    assert by_turn["t0004"] == "item-2"
    assert by_turn["t0008"] == "item-2"


def test_chunk_dict_with_agenda_id_is_deterministic():
    transcript = (
        "CHAIR: Item 1: first.\n"
        "ALICE: discussing.\n"
        "CHAIR: Item 2: second.\n"
    )
    a = chunk_transcript(transcript)
    b = chunk_transcript(transcript)
    assert a == b
