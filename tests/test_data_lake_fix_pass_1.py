"""Regression tests for SSC-010 (fix pass after Red Team #1)."""
import json

import pytest

from spectrum_systems_core.data_lake import (
    WriterError,
    run_transcript_pipeline,
    write_promoted_artifact,
)


VALID_METADATA = {
    "meeting_id": "m-fix1",
    "title": "Fix pass case",
    "date": "2026-05-09",
    "source_type": "transcript",
    "agency": "FCC",
    "topic": "3.5 GHz sharing",
}

OK_TRANSCRIPT = (
    "Fix pass case\n"
    "DECISION: Approve.\n"
    "ACTION: Draft note.\n"
    "QUESTION: Reviewer?\n"
)

EMPTY_HEADER_TRANSCRIPT = "Just a header line\nMore prose without prefixes.\n"


def _setup(tmp_path, transcript=OK_TRANSCRIPT, metadata=None):
    metadata = metadata or VALID_METADATA
    d = tmp_path / "raw" / "meetings" / metadata["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


# --- M1, M3: byte-deterministic envelope and manifest -----------------


def test_processed_artifact_file_is_byte_identical_across_runs(tmp_path, tmp_path_factory):
    _setup(tmp_path)
    a = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    other = tmp_path_factory.mktemp("other")
    _setup(other)
    b = run_transcript_pipeline(
        lake_root=other, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )

    from pathlib import Path
    a_file = Path(a.written_paths[0])
    b_file = Path(b.written_paths[0])
    assert a_file.read_bytes() == b_file.read_bytes()


def test_manifest_file_is_byte_identical_across_runs(tmp_path, tmp_path_factory):
    _setup(tmp_path)
    a = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    other = tmp_path_factory.mktemp("other")
    _setup(other)
    b = run_transcript_pipeline(
        lake_root=other, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    from pathlib import Path
    assert Path(a.manifest_path).read_bytes() == Path(b.manifest_path).read_bytes()


def test_artifact_id_and_created_at_are_stable_inside_pipeline(tmp_path, tmp_path_factory):
    _setup(tmp_path)
    a = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    other = tmp_path_factory.mktemp("other")
    _setup(other)
    b = run_transcript_pipeline(
        lake_root=other, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    assert a.target.artifact_id == b.target.artifact_id
    assert a.target.created_at == b.target.created_at
    assert a.control_decision.artifact_id == b.control_decision.artifact_id


# --- M2: deterministic slug fallback ---------------------------------


def test_slug_fallback_is_deterministic_from_content_hash(tmp_path, tmp_path_factory):
    _setup(tmp_path)
    a = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    other = tmp_path_factory.mktemp("other")
    _setup(other)
    b = run_transcript_pipeline(
        lake_root=other, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    from pathlib import Path
    assert Path(a.written_paths[0]).name == Path(b.written_paths[0]).name


# --- M4: writer rejects '__' inside slug -----------------------------


def test_writer_rejects_double_underscore_in_slug(tmp_path):
    from spectrum_systems_core.artifacts import new_artifact
    art = new_artifact(
        "meeting_minutes",
        {"meeting_id": "m-1", "title": "x"},
        trace_id="t",
        status="draft",
    )
    art.status = "promoted"
    with pytest.raises(WriterError, match="must not contain '__'"):
        write_promoted_artifact(tmp_path, art, slug="bad__slug")


# --- S1: optional metadata is in debug report ------------------------


def test_debug_report_includes_optional_metadata_fields(tmp_path):
    _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    optional = result.debug_report["input"]["optional_metadata"]
    assert optional["agency"] == "FCC"
    assert optional["topic"] == "3.5 GHz sharing"
    # Required fields should NOT be duplicated into optional_metadata
    assert "title" not in optional
    assert "meeting_id" not in optional


# --- S2: transcript_evidence eval blocks no-evidence runs ------------


def test_transcript_evidence_blocks_when_no_grounded_spans(tmp_path):
    _setup(tmp_path, transcript=EMPTY_HEADER_TRANSCRIPT)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    assert result.promoted is False
    assert result.control_decision.payload["decision"] == "block"
    reasons = result.control_decision.payload["reason_codes"]
    assert any("transcript_evidence" in r for r in reasons)


def test_transcript_evidence_passes_when_grounding_present(tmp_path):
    _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    assert result.promoted is True
    evidence_eval = next(
        e for e in result.eval_results
        if e.payload.get("eval_type") == "transcript_evidence"
    )
    assert evidence_eval.payload["status"] == "pass"


def test_transcript_evidence_does_not_apply_to_non_transcript_sources(tmp_path):
    """source_type 'notes' is not held to the grounded-evidence bar."""
    meta = dict(VALID_METADATA)
    meta["source_type"] = "notes"
    _setup(tmp_path, transcript=EMPTY_HEADER_TRANSCRIPT, metadata=meta)
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-fix1", workflow_name="meeting_minutes"
    )
    evidence_eval = next(
        e for e in result.eval_results
        if e.payload.get("eval_type") == "transcript_evidence"
    )
    assert evidence_eval.payload["status"] == "pass"
