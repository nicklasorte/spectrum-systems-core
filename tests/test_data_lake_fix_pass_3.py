"""Regression tests for SSC-018 (fix pass after Red Team #3)."""
import json
import shutil
from pathlib import Path

from spectrum_systems_core.data_lake import (
    build_grounded_payload,
    load_meeting,
    run_transcript_pipeline,
)
from spectrum_systems_core.data_lake.paths import (
    debug_filename,
    is_run_metadata_filename,
    manifest_filename,
)

FIXTURES = Path(__file__).parent / "fixtures" / "golden_meetings"


# --- M1: extract.py duplication is gone (behavior preserved) ---------


def test_grounding_still_produces_correct_kinds_for_meeting_minutes(tmp_path):
    d = tmp_path / "raw" / "meetings" / "m-1"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "Header\nDECISION: x\nACTION: y\nQUESTION: z\n", encoding="utf-8"
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-1", "title": "t", "date": "2026-05-09",
            "source_type": "transcript",
        }),
        encoding="utf-8",
    )
    ti = load_meeting(tmp_path, "m-1")
    payload = build_grounded_payload(ti, "meeting_minutes")
    kinds = sorted(g["kind"] for g in payload["grounding"])
    assert kinds == ["action_item", "decision", "open_question"]


def test_grounding_still_produces_correct_kinds_for_decision_brief(tmp_path):
    d = tmp_path / "raw" / "meetings" / "m-1"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "Brief\nCONTEXT: c\nOPTION: a\nRECOMMENDATION: r\nRATIONALE: w\n",
        encoding="utf-8",
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-1", "title": "t", "date": "2026-05-09",
            "source_type": "transcript",
        }),
        encoding="utf-8",
    )
    ti = load_meeting(tmp_path, "m-1")
    payload = build_grounded_payload(ti, "decision_brief")
    kinds = sorted(g["kind"] for g in payload["grounding"])
    assert kinds == ["context", "option", "rationale", "recommendation"]


# --- S1: filename helpers centralize convention ----------------------


def test_manifest_and_debug_filenames_use_helpers(tmp_path):
    d = tmp_path / "raw" / "meetings" / "m-h"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "Header\nDECISION: x\n", encoding="utf-8"
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-h", "title": "t", "date": "2026-05-09",
            "source_type": "transcript",
        }),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-h", workflow_name="meeting_minutes"
    )
    assert Path(result.manifest_path).name == manifest_filename(result.run_id)
    assert Path(result.debug_path).name == debug_filename(result.run_id)


def test_is_run_metadata_filename_recognizes_both_prefixes():
    assert is_run_metadata_filename("manifest__abc.json")
    assert is_run_metadata_filename("debug__abc.json")
    assert not is_run_metadata_filename("meeting_minutes__abc.json")


# --- S2: content_signal blocks empty notes/summary -------------------


def test_content_signal_blocks_empty_notes_source(tmp_path):
    d = tmp_path / "raw" / "meetings" / "m-notes"
    d.mkdir(parents=True)
    # No DECISION/ACTION/QUESTION lines and source_type=notes -> empty content.
    # Speaker-labelled lines so the Phase Y chunker gate (no_speaker_structure
    # on 100%-null-speaker transcripts) does not pre-empt content_signal.
    (d / "transcript.txt").write_text(
        "ALICE: Some chatter\nBOB: Nothing actionable here.\n",
        encoding="utf-8",
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-notes", "title": "Casual", "date": "2026-05-09",
            "source_type": "notes",
        }),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-notes", workflow_name="meeting_minutes"
    )
    assert result.promoted is False
    reasons = result.control_decision.payload["reason_codes"]
    assert any("content_signal" in r for r in reasons)


def test_content_signal_passes_when_notes_source_has_real_content(tmp_path):
    d = tmp_path / "raw" / "meetings" / "m-notes"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "Header\nDECISION: x\n", encoding="utf-8"
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-notes", "title": "x", "date": "2026-05-09",
            "source_type": "notes",
        }),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-notes", workflow_name="meeting_minutes"
    )
    content_eval = next(
        e for e in result.eval_results
        if e.payload.get("eval_type") == "content_signal"
    )
    assert content_eval.payload["status"] == "pass"


def test_content_signal_does_not_apply_to_transcript_source(tmp_path):
    """transcript_evidence handles transcript sources; content_signal must not double-block."""
    d = tmp_path / "raw" / "meetings" / "m-tx"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "Header\nDECISION: x\n", encoding="utf-8"
    )
    (d / "metadata.json").write_text(
        json.dumps({
            "meeting_id": "m-tx", "title": "x", "date": "2026-05-09",
            "source_type": "transcript",
        }),
        encoding="utf-8",
    )
    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id="m-tx", workflow_name="meeting_minutes"
    )
    content_eval = next(
        e for e in result.eval_results
        if e.payload.get("eval_type") == "content_signal"
    )
    assert content_eval.payload["status"] == "pass"


# --- S3: agency_question_summary golden fixture ----------------------


def test_golden_inquiry_promotes_with_expected_payload(tmp_path):
    meeting_id = "m-golden-inquiry"
    src = FIXTURES / meeting_id
    dst = tmp_path / "raw" / "meetings" / meeting_id
    dst.mkdir(parents=True)
    shutil.copy(src / "transcript.txt", dst / "transcript.txt")
    shutil.copy(src / "metadata.json", dst / "metadata.json")
    expected = json.loads((src / "expected.json").read_text(encoding="utf-8"))

    result = run_transcript_pipeline(
        lake_root=tmp_path, meeting_id=meeting_id,
        workflow_name="agency_question_summary",
    )
    assert result.promoted is expected["promoted"]
    assert result.target.artifact_type == expected["artifact_type"]
    assert result.control_decision.payload["decision"] == expected["decision"]
    payload = result.target.payload
    assert payload["agency"] == expected["agency"]
    assert payload["citations"] == expected["citations"]
    grounding_kinds = {g["kind"] for g in payload["grounding"]}
    for kind in expected["grounding_kinds_include"]:
        assert kind in grounding_kinds
