import json
from pathlib import Path

from spectrum_systems_core.data_lake import run_transcript_pipeline

VALID_METADATA = {
    "meeting_id": "m-debug-1",
    "title": "Debug case",
    "date": "2026-05-09",
    "source_type": "transcript",
}

OK_TRANSCRIPT = (
    "Debug case\n"
    "DECISION: Go.\n"
    "ACTION: Do.\n"
    "QUESTION: When?\n"
)


def _setup(tmp_path, transcript=OK_TRANSCRIPT, metadata=None):
    metadata = metadata or VALID_METADATA
    d = tmp_path / "raw" / "meetings" / metadata["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def test_debug_report_explains_allow_case(tmp_path):
    _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-debug-1", workflow_name="meeting_minutes"
    )
    assert result.promoted is True
    rep = result.debug_report
    assert rep["outcome"] == "promoted"
    assert rep["control"]["decision"] == "allow"
    assert "allowed because all required evals passed" in rep["control"]["explanation"]
    assert rep["written_paths"], "should record at least one write"
    assert rep["produced_artifact"]["artifact_id"] == result.target.artifact_id


def test_debug_report_explains_block_case(tmp_path, monkeypatch):
    _setup(tmp_path)

    # Force block by zeroing required fields after extraction.
    from spectrum_systems_core.data_lake import extract as ext

    def empty_extract(_text):
        return {}

    monkeypatch.setitem(
        ext.GROUNDED_EXTRACTORS,
        "meeting_minutes",
        (empty_extract, ext.GROUNDED_EXTRACTORS["meeting_minutes"][1]),
    )

    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-debug-1", workflow_name="meeting_minutes"
    )
    assert result.promoted is False
    rep = result.debug_report
    assert rep["outcome"] == "rejected"
    assert rep["control"]["decision"] == "block"
    assert rep["control"]["reason_codes"], "block must list reason codes"
    assert any(
        rw["artifact_id"] == result.target.artifact_id for rw in rep["rejected_writes"]
    )
    assert rep["written_paths"] == []


def test_debug_report_has_artifact_ids_and_decision(tmp_path):
    _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-debug-1", workflow_name="meeting_minutes"
    )
    rep = result.debug_report
    assert rep["produced_artifact"]["artifact_id"] == result.target.artifact_id
    assert rep["control"]["artifact_id"] == result.control_decision.artifact_id
    assert rep["meeting_id"] == "m-debug-1"
    assert rep["evals"]["passed"]
    eval_types = [e["eval_type"] for e in rep["evals"]["passed"]]
    assert "non_empty_payload" in eval_types
    assert "source_grounding" in eval_types


def test_debug_report_is_written_to_disk(tmp_path):
    _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-debug-1", workflow_name="meeting_minutes"
    )
    assert result.debug_path is not None
    p = Path(result.debug_path)
    assert p.is_file()
    body = json.loads(p.read_text(encoding="utf-8"))
    assert body == result.debug_report
