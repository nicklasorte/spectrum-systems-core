import json
from pathlib import Path

from spectrum_systems_core.data_lake import (
    artifact_index_path,
    collect_index_records,
    read_artifact_index,
    run_transcript_pipeline,
    write_artifact_index,
    write_promoted_artifact,
)
from spectrum_systems_core.artifacts import new_artifact


M1_TRANSCRIPT = (
    "Q3 planning sync\n"
    "DECISION: Approve Q3 plan.\n"
    "ACTION: Draft SSC-002 docs.\n"
)
M1_META = {
    "meeting_id": "m-2026-05-09-q3",
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
    "meeting_id": "m-2026-04-01-bands",
    "title": "Band review",
    "date": "2026-04-01",
    "source_type": "transcript",
    "agency": "NTIA",
    "topic": "C-band",
}


def _seed_meeting(lake_root, meeting_id, transcript, metadata):
    d = lake_root / "raw" / "meetings" / meeting_id
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _run_two_meetings(tmp_path):
    _seed_meeting(tmp_path, M1_META["meeting_id"], M1_TRANSCRIPT, M1_META)
    _seed_meeting(tmp_path, M2_META["meeting_id"], M2_TRANSCRIPT, M2_META)
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M1_META["meeting_id"], workflow_name="meeting_minutes"
    )
    run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=M2_META["meeting_id"], workflow_name="meeting_minutes"
    )


def test_index_builds_from_multiple_processed_meetings(tmp_path):
    _run_two_meetings(tmp_path)
    out = write_artifact_index(tmp_path)
    assert out.is_file()
    records = read_artifact_index(tmp_path)
    assert len(records) == 2
    meeting_ids = {r["meeting_id"] for r in records}
    assert meeting_ids == {M1_META["meeting_id"], M2_META["meeting_id"]}


def test_index_skips_non_promoted_artifacts(tmp_path):
    _run_two_meetings(tmp_path)
    # Manually drop a non-promoted artifact file into processed/meetings/
    bad_dir = tmp_path / "processed" / "meetings" / M1_META["meeting_id"]
    fake = {
        "artifact_id": "fake-1",
        "artifact_type": "meeting_minutes",
        "schema_version": 1,
        "status": "draft",
        "created_at": "2026-05-09T00:00:00+00:00",
        "trace_id": "trace-x",
        "input_refs": [],
        "content_hash": "0" * 64,
        "payload": {"meeting_id": M1_META["meeting_id"], "title": "draft"},
    }
    (bad_dir / "meeting_minutes__draft.json").write_text(json.dumps(fake), encoding="utf-8")

    write_artifact_index(tmp_path)
    records = read_artifact_index(tmp_path)
    assert all(r["artifact_id"] != "fake-1" for r in records)


def test_index_skips_manifest_and_debug_files(tmp_path):
    _run_two_meetings(tmp_path)
    write_artifact_index(tmp_path)
    records = read_artifact_index(tmp_path)
    for r in records:
        assert not Path(r["path"]).name.startswith("manifest__")
        assert not Path(r["path"]).name.startswith("debug__")


def test_index_is_byte_deterministic(tmp_path, tmp_path_factory):
    _run_two_meetings(tmp_path)
    write_artifact_index(tmp_path)
    a_bytes = artifact_index_path(tmp_path).read_bytes()

    other = tmp_path_factory.mktemp("other")
    _run_two_meetings(other)
    write_artifact_index(other)
    b_bytes = artifact_index_path(other).read_bytes()

    assert a_bytes == b_bytes


def test_index_records_have_required_fields(tmp_path):
    _run_two_meetings(tmp_path)
    write_artifact_index(tmp_path)
    records = read_artifact_index(tmp_path)
    for r in records:
        for key in ("meeting_id", "date", "artifact_id", "artifact_type", "title", "path"):
            assert key in r, f"missing required index field: {key}"


def test_index_includes_optional_fields_when_available(tmp_path):
    _run_two_meetings(tmp_path)
    write_artifact_index(tmp_path)
    records = read_artifact_index(tmp_path)
    by_meeting = {r["meeting_id"]: r for r in records}
    m1 = by_meeting[M1_META["meeting_id"]]
    assert m1.get("agency") == "FCC"
    assert m1.get("topic") == "3.5 GHz"
    assert isinstance(m1.get("source_excerpt"), str) and m1["source_excerpt"]


def test_index_path_is_relative_to_lake_root(tmp_path):
    _run_two_meetings(tmp_path)
    write_artifact_index(tmp_path)
    records = read_artifact_index(tmp_path)
    for r in records:
        assert not r["path"].startswith("/")
        assert r["path"].startswith("processed/meetings/")


def test_index_ordering_is_stable_by_meeting_then_type_then_id(tmp_path):
    _run_two_meetings(tmp_path)
    records = collect_index_records(tmp_path)
    keys = [(r.meeting_id, r.artifact_type, r.artifact_id) for r in records]
    assert keys == sorted(keys)


def test_index_empty_when_no_processed_dir(tmp_path):
    records = collect_index_records(tmp_path)
    assert records == []
    write_artifact_index(tmp_path)
    assert artifact_index_path(tmp_path).read_text() == ""


def test_index_skips_files_that_are_not_artifact_envelopes(tmp_path):
    _run_two_meetings(tmp_path)
    bad_dir = tmp_path / "processed" / "meetings" / M1_META["meeting_id"]
    (bad_dir / "meeting_minutes__not-json.json").write_text("not json", encoding="utf-8")
    (bad_dir / "meeting_minutes__no-payload.json").write_text(
        json.dumps({"artifact_type": "meeting_minutes"}), encoding="utf-8"
    )
    records = collect_index_records(tmp_path)
    # Both junk files should be ignored without raising.
    assert all("not-json" not in r.path and "no-payload" not in r.path for r in records)
