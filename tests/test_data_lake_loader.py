import json

import pytest

from spectrum_systems_core.data_lake import (
    LoaderError,
    TranscriptInput,
    load_meeting,
    load_meeting_from_dir,
)

VALID_METADATA = {
    "meeting_id": "m-2026-05-09-alpha",
    "title": "Alpha planning",
    "date": "2026-05-09",
    "source_type": "transcript",
    "agency": "FCC",
}

VALID_TRANSCRIPT = (
    "Alpha planning sync\n"
    "DECISION: Approve plan A.\n"
    "ACTION: Draft response by Friday.\n"
    "QUESTION: Who reviews?\n"
)


def _write_meeting(lake_root, meeting_id, transcript=None, metadata=None):
    meeting_dir = lake_root / "raw" / "meetings" / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    if transcript is not None:
        (meeting_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
    if metadata is not None:
        (meeting_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
    return meeting_dir


def test_load_meeting_returns_transcript_input(tmp_path):
    _write_meeting(tmp_path, "m-2026-05-09-alpha", VALID_TRANSCRIPT, VALID_METADATA)

    loaded = load_meeting(tmp_path, "m-2026-05-09-alpha")

    assert isinstance(loaded, TranscriptInput)
    assert loaded.meeting_id == "m-2026-05-09-alpha"
    assert loaded.title == "Alpha planning"
    assert loaded.date == "2026-05-09"
    assert loaded.source_type == "transcript"
    assert loaded.transcript_text == VALID_TRANSCRIPT
    assert loaded.transcript_lines[1] == "DECISION: Approve plan A."
    assert loaded.metadata["agency"] == "FCC"
    assert len(loaded.transcript_hash) == 64
    assert len(loaded.metadata_hash) == 64


def test_load_meeting_rejects_missing_transcript(tmp_path):
    _write_meeting(tmp_path, "m-1", transcript=None, metadata=VALID_METADATA | {"meeting_id": "m-1"})
    with pytest.raises(LoaderError, match="missing transcript"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_missing_metadata(tmp_path):
    _write_meeting(tmp_path, "m-1", transcript=VALID_TRANSCRIPT, metadata=None)
    with pytest.raises(LoaderError, match="missing metadata"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_invalid_json(tmp_path):
    meeting_dir = tmp_path / "raw" / "meetings" / "m-1"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "transcript.txt").write_text(VALID_TRANSCRIPT)
    (meeting_dir / "metadata.json").write_text("{not: valid json")
    with pytest.raises(LoaderError, match="invalid JSON"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_empty_transcript(tmp_path):
    _write_meeting(tmp_path, "m-1", transcript="\n\n", metadata=VALID_METADATA | {"meeting_id": "m-1"})
    with pytest.raises(LoaderError, match="transcript is empty"):
        load_meeting(tmp_path, "m-1")


@pytest.mark.parametrize(
    "missing_field", ["meeting_id", "title", "date", "source_type"]
)
def test_load_meeting_rejects_missing_required_metadata_field(tmp_path, missing_field):
    meta = dict(VALID_METADATA)
    meta["meeting_id"] = "m-1"
    del meta[missing_field]
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, meta)
    with pytest.raises(LoaderError, match=f"required field: {missing_field}"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_blank_required_metadata_field(tmp_path):
    meta = dict(VALID_METADATA)
    meta["meeting_id"] = "m-1"
    meta["title"] = "   "
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, meta)
    with pytest.raises(LoaderError, match="title must be a non-empty string"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_meeting_id_mismatch(tmp_path):
    meta = dict(VALID_METADATA)
    meta["meeting_id"] = "m-other"
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, meta)
    with pytest.raises(LoaderError, match="does not match"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_invalid_date(tmp_path):
    meta = dict(VALID_METADATA)
    meta["meeting_id"] = "m-1"
    meta["date"] = "May 9 2026"
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, meta)
    with pytest.raises(LoaderError, match="YYYY-MM-DD"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_invalid_source_type(tmp_path):
    meta = dict(VALID_METADATA)
    meta["meeting_id"] = "m-1"
    meta["source_type"] = "podcast"
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, meta)
    with pytest.raises(LoaderError, match="source_type"):
        load_meeting(tmp_path, "m-1")


def test_load_meeting_rejects_invalid_meeting_id(tmp_path):
    with pytest.raises(ValueError, match="invalid meeting_id"):
        load_meeting(tmp_path, "Bad Meeting!")


def test_load_meeting_from_dir_works(tmp_path):
    meeting_dir = _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, VALID_METADATA | {"meeting_id": "m-1"})
    loaded = load_meeting_from_dir(meeting_dir)
    assert loaded.meeting_id == "m-1"


def test_loader_hashes_are_deterministic(tmp_path, tmp_path_factory):
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, VALID_METADATA | {"meeting_id": "m-1"})
    a = load_meeting(tmp_path, "m-1")

    other_root = tmp_path_factory.mktemp("other")
    _write_meeting(other_root, "m-1", VALID_TRANSCRIPT, VALID_METADATA | {"meeting_id": "m-1"})
    b = load_meeting(other_root, "m-1")

    assert a.transcript_hash == b.transcript_hash
    assert a.metadata_hash == b.metadata_hash


def test_loader_line_helper_indexes_one_based(tmp_path):
    _write_meeting(tmp_path, "m-1", VALID_TRANSCRIPT, VALID_METADATA | {"meeting_id": "m-1"})
    loaded = load_meeting(tmp_path, "m-1")
    assert loaded.line(1) == "Alpha planning sync"
    assert loaded.line(2) == "DECISION: Approve plan A."
    with pytest.raises(IndexError):
        loaded.line(0)
    with pytest.raises(IndexError):
        loaded.line(99)
