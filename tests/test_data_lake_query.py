import json

import pytest

from spectrum_systems_core.data_lake import (
    QueryError,
    QueryResult,
    query,
    run_transcript_pipeline,
    write_artifact_index,
)

M1_TRANSCRIPT = (
    "Q3 planning sync\n"
    "DECISION: Approve Q3 plan.\n"
    "ACTION: Draft SSC-002 docs.\n"
)
M1_META = {
    "meeting_id": "m-q3",
    "title": "Q3 planning sync",
    "date": "2026-05-09",
    "source_type": "transcript",
    "agency": "FCC",
    "topic": "3.5 GHz",
}

M2_TRANSCRIPT = (
    "Spectrum band review\n"
    "DECISION: Adopt option B.\n"
    "QUESTION: When do we file?\n"
)
M2_META = {
    "meeting_id": "m-bands",
    "title": "Band review",
    "date": "2026-04-01",
    "source_type": "transcript",
    "agency": "NTIA",
    "topic": "C-band",
}

M3_TEXT = (
    "Inquiry response\n"
    "AGENCY: FCC\n"
    "QUESTION: What is the proposed sharing rule for 3.5 GHz?\n"
    "CITATION: 47 CFR 96.41\n"
)
M3_META = {
    "meeting_id": "m-inquiry",
    "title": "FCC inquiry on band plan",
    "date": "2026-03-15",
    "source_type": "transcript",
    "agency": "FCC",
    "topic": "3.5 GHz",
}


def _seed(lake_root, meeting_id, transcript, meta):
    d = lake_root / "raw" / "meetings" / meeting_id
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def _seed_three(tmp_path):
    _seed(tmp_path, M1_META["meeting_id"], M1_TRANSCRIPT, M1_META)
    _seed(tmp_path, M2_META["meeting_id"], M2_TRANSCRIPT, M2_META)
    _seed(tmp_path, M3_META["meeting_id"], M3_TEXT, M3_META)
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M1_META["meeting_id"], workflow_name="meeting_minutes"
    )
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M2_META["meeting_id"], workflow_name="meeting_minutes"
    )
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M3_META["meeting_id"],
        workflow_name="agency_question_summary",
    )
    write_artifact_index(tmp_path)


def test_query_by_agency_returns_only_matching_meetings(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, agency="FCC")
    meeting_ids = sorted(r.record["meeting_id"] for r in results)
    assert meeting_ids == ["m-inquiry", "m-q3"]


def test_query_by_artifact_type_filters_correctly(tmp_path):
    _seed_three(tmp_path)
    minutes = query(tmp_path, artifact_type="meeting_minutes")
    assert sorted(r.record["meeting_id"] for r in minutes) == ["m-bands", "m-q3"]
    inquiries = query(tmp_path, artifact_type="agency_question_summary")
    assert [r.record["meeting_id"] for r in inquiries] == ["m-inquiry"]


def test_query_by_meeting_id_filters_correctly(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, meeting_id="m-q3")
    assert len(results) == 1
    assert results[0].record["title"] == "Q3 planning sync"


def test_query_by_keyword_matches_title(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, keyword="band")
    # Title is the first transcript line; M2 has "Spectrum band review",
    # M3 has "Inquiry response" (no 'band' in title; it'll match payload).
    titles = sorted(r.record["title"] for r in results)
    assert "Spectrum band review" in titles


def test_query_by_keyword_matches_source_excerpt(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, keyword="DECISION: Adopt option B")
    assert any(r.record["meeting_id"] == "m-bands" for r in results)
    matched = [r for r in results if r.record["meeting_id"] == "m-bands"][0]
    assert "source_excerpt" in matched.matched_fields or "payload_text" in matched.matched_fields


def test_query_by_keyword_matches_payload_text(tmp_path):
    _seed_three(tmp_path)
    # 'sharing rule' appears in the FCC inquiry's payload but not in title or source_excerpt
    results = query(tmp_path, keyword="sharing rule")
    assert any(r.record["meeting_id"] == "m-inquiry" for r in results)


def test_query_keyword_is_case_insensitive(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, keyword="BAND")
    assert any(r.record["meeting_id"] == "m-bands" for r in results)


def test_query_date_filter_includes_only_in_range(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, date_from="2026-04-01", date_to="2026-12-31")
    ids = sorted(r.record["meeting_id"] for r in results)
    assert ids == ["m-bands", "m-q3"]


def test_query_date_filter_lower_bound_only(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, date_from="2026-05-01")
    ids = [r.record["meeting_id"] for r in results]
    assert ids == ["m-q3"]


def test_query_date_filter_upper_bound_only(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, date_to="2026-03-31")
    ids = [r.record["meeting_id"] for r in results]
    assert ids == ["m-inquiry"]


def test_query_combines_filters(tmp_path):
    _seed_three(tmp_path)
    results = query(
        tmp_path,
        agency="FCC",
        artifact_type="meeting_minutes",
        keyword="Q3",
    )
    assert [r.record["meeting_id"] for r in results] == ["m-q3"]


def test_query_unsupported_filter_fails_clearly(tmp_path):
    _seed_three(tmp_path)
    with pytest.raises(QueryError, match="unsupported filter"):
        # Using a real Python kwarg through the typed signature is tricky;
        # we exercise the path that validates filter names directly.
        from spectrum_systems_core.data_lake.query import _validate_filters
        _validate_filters(vector="anything")


def test_query_returns_query_result_objects(tmp_path):
    _seed_three(tmp_path)
    results = query(tmp_path, agency="FCC")
    assert all(isinstance(r, QueryResult) for r in results)


def test_query_empty_when_no_index(tmp_path):
    assert query(tmp_path, agency="FCC") == []


def test_query_no_match_returns_empty_list(tmp_path):
    _seed_three(tmp_path)
    assert query(tmp_path, agency="DOD") == []
