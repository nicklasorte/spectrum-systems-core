"""Regression tests for SSC-014 (fix pass after Red Team #2)."""
import json

import pytest

from spectrum_systems_core.data_lake import (
    QueryError,
    artifact_index_path,
    query,
    run_transcript_pipeline,
)


M1_TRANSCRIPT = (
    "Q3 planning sync\n"
    "DECISION: Approve plan.\n"
    "ACTION: Draft note.\n"
)
M1_META = {
    "meeting_id": "m-q3",
    "title": "Q3 planning sync",
    "date": "2026-05-09",
    "source_type": "transcript",
    "agency": "FCC",
    "topic": "3.5 GHz",
}


def _seed(tmp_path):
    d = tmp_path / "raw" / "meetings" / M1_META["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(M1_TRANSCRIPT, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(M1_META), encoding="utf-8")
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M1_META["meeting_id"], workflow_name="meeting_minutes"
    )


# --- KW1: keyword does not match JSON encoding structure --------------


def test_keyword_does_not_match_json_field_names(tmp_path):
    """Keyword 'grounding' must not match because 'grounding' is a field name,
    not text content."""
    _seed(tmp_path)
    results = query(tmp_path, keyword="grounding")
    assert results == [], "field name 'grounding' should not produce a match"


def test_keyword_does_not_match_meeting_id_field_name(tmp_path):
    _seed(tmp_path)
    # 'meeting_id' is a key — should not produce a match by itself
    # (the value m-q3 of course matches if the keyword is m-q3)
    results = query(tmp_path, keyword="meeting_id")
    assert results == []


def test_keyword_still_matches_real_string_content(tmp_path):
    _seed(tmp_path)
    results = query(tmp_path, keyword="approve plan")
    assert len(results) == 1


# --- DT1: date filter validation -------------------------------------


def test_query_rejects_non_yyyy_mm_dd_date_from(tmp_path):
    _seed(tmp_path)
    with pytest.raises(QueryError, match="date_from"):
        query(tmp_path, date_from="May 9 2026")


def test_query_rejects_non_yyyy_mm_dd_date_to(tmp_path):
    _seed(tmp_path)
    with pytest.raises(QueryError, match="date_to"):
        query(tmp_path, date_to="2026/12/31")


def test_query_accepts_valid_yyyy_mm_dd_dates(tmp_path):
    _seed(tmp_path)
    out = query(tmp_path, date_from="2026-01-01", date_to="2026-12-31")
    assert len(out) == 1


# --- IDX1: query auto-builds index when missing -----------------------


def test_query_builds_index_on_demand_when_missing(tmp_path):
    _seed(tmp_path)
    # Remove index file so query has to rebuild
    idx = artifact_index_path(tmp_path)
    if idx.is_file():
        idx.unlink()
    assert not idx.is_file()
    results = query(tmp_path, agency="FCC")
    assert idx.is_file(), "query must rebuild missing index"
    assert len(results) == 1
