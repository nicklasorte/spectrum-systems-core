"""Regression tests for must_fix items recorded in
docs/reviews/ssc_next_memory_redteam_1.md.

Each test names the finding it defends.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.data_lake import (
    AGENCIES_SUBDIR,
    INDEX_FILENAME,
    TOPICS_SUBDIR,
    artifact_markdown_path,
    markdown_dir,
    process_meeting,
    render_artifact_markdown,
)
from spectrum_systems_core.data_lake.loader import TranscriptInput

FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


def _seed(lake_root: Path, meeting_id: str) -> None:
    src = FIXTURES / meeting_id
    dst = lake_root / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")


def _stub_transcript_input(meeting_id: str) -> TranscriptInput:
    return TranscriptInput(
        meeting_id=meeting_id,
        title="Demo",
        date="2026-05-09",
        source_type="transcript",
        transcript_text="Demo\n",
        transcript_lines=("Demo",),
        metadata={
            "meeting_id": meeting_id,
            "title": "Demo",
            "date": "2026-05-09",
            "source_type": "transcript",
        },
        transcript_hash="t-hash",
        metadata_hash="m-hash",
        transcript_path="/tmp/demo/transcript.txt",
        metadata_path="/tmp/demo/metadata.json",
    )


def test_M1_canonical_json_path_uses_unwritten_sentinel_when_unknown():
    """M1: frontmatter never claims an empty JSON path."""
    artifact = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "Demo",
            "summary": "demo",
            "decisions": ["x"],
            "action_items": [],
            "open_questions": [],
        },
        trace_id="trace-demo",
        status="promoted",
    )
    text = render_artifact_markdown(
        artifact, transcript_input=_stub_transcript_input("m-demo")
    )
    # Frontmatter has the field, with the explicit sentinel value.
    assert "canonical_json_path: (unwritten)" in text
    # Body links section explains the sentinel in plain English.
    assert "the artifact has not been promoted to disk" in text


def test_M2_artifact_markdown_says_json_is_canonical(tmp_path):
    """M2: the boundary message is unambiguous."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    md = artifact_markdown_path(tmp_path, meeting_id, "meeting_minutes")
    text = md.read_text(encoding="utf-8")
    assert "JSON is the canonical source of truth for this artifact" in text
    assert "core never reads it back" in text


def test_M3_index_body_states_canonical(tmp_path):
    """M3: a reader scanning index.md body sees the canonical hint."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    text = (markdown_dir(tmp_path, meeting_id) / INDEX_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "JSON is canonical" in text
    assert "regenerated views" in text


def test_S1_agency_note_includes_original_string(tmp_path):
    """S1: agency note shows original string, not just the slug."""
    meeting_id = "m-golden-inquiry"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    p = (
        markdown_dir(tmp_path, meeting_id)
        / AGENCIES_SUBDIR
        / "fcc.md"
    )
    text = p.read_text(encoding="utf-8")
    assert "Original agency string: `FCC`" in text


def test_S1_topic_note_includes_original_string(tmp_path):
    """S1 (extended): topic note shows original string too."""
    meeting_id = "m-golden-good"
    _seed(tmp_path, meeting_id)
    process_meeting(lake_root=tmp_path, meeting_id=meeting_id)
    topic_dir = markdown_dir(tmp_path, meeting_id) / TOPICS_SUBDIR
    files = list(topic_dir.glob("*.md"))
    assert files
    text = files[0].read_text(encoding="utf-8")
    assert "Original topic string:" in text
