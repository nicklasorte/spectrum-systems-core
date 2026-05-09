import json

import pytest

from spectrum_systems_core.data_lake import (
    build_grounded_payload,
    evaluate_grounding,
    excerpt_is_in_transcript,
    load_meeting,
    run_transcript_pipeline,
)


SAMPLE_TRANSCRIPT = (
    "Q3 planning sync\n"
    "We reviewed roadmap and budget.\n"
    "DECISION: Approve Q3 plan.\n"
    "ACTION: Draft SSC-002 docs.\n"
    "QUESTION: Do we add an empty-transcript eval?\n"
)

VALID_METADATA = {
    "meeting_id": "m-q3-2026",
    "title": "Q3 planning sync",
    "date": "2026-05-09",
    "source_type": "transcript",
    "agency": "FCC",
}


def _setup(tmp_path, transcript=SAMPLE_TRANSCRIPT, metadata=None):
    metadata = metadata or VALID_METADATA
    d = tmp_path / "raw" / "meetings" / metadata["meeting_id"]
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return load_meeting(tmp_path, metadata["meeting_id"])


def test_grounded_payload_includes_meeting_id_and_grounding(tmp_path):
    ti = _setup(tmp_path)
    payload = build_grounded_payload(ti, "meeting_minutes")
    assert payload["meeting_id"] == "m-q3-2026"
    assert isinstance(payload["grounding"], list)
    assert len(payload["grounding"]) >= 3
    kinds = {g["kind"] for g in payload["grounding"]}
    assert {"decision", "action_item", "open_question"} <= kinds


def test_grounded_payload_excerpts_exist_in_transcript(tmp_path):
    ti = _setup(tmp_path)
    payload = build_grounded_payload(ti, "meeting_minutes")
    for g in payload["grounding"]:
        assert g["source_excerpt"] in ti.transcript_text
        assert 1 <= g["start_line"] <= len(ti.transcript_lines)
        assert g["end_line"] >= g["start_line"]


def test_grounded_payload_line_numbers_are_correct(tmp_path):
    ti = _setup(tmp_path)
    payload = build_grounded_payload(ti, "meeting_minutes")
    by_kind = {g["kind"]: g for g in payload["grounding"] if g["kind"] in {"decision", "action_item", "open_question"}}
    assert by_kind["decision"]["start_line"] == 3
    assert by_kind["action_item"]["start_line"] == 4
    assert by_kind["open_question"]["start_line"] == 5


def test_grounded_payload_unsupported_workflow_raises(tmp_path):
    ti = _setup(tmp_path)
    with pytest.raises(ValueError, match="unsupported workflow_name"):
        build_grounded_payload(ti, "not_a_workflow")


def test_evaluate_grounding_passes_when_excerpts_match(tmp_path):
    ti = _setup(tmp_path)
    payload = build_grounded_payload(ti, "meeting_minutes")
    passed, reasons = evaluate_grounding(ti, payload)
    assert passed is True
    assert reasons == []


def test_evaluate_grounding_fails_when_excerpt_not_in_transcript(tmp_path):
    ti = _setup(tmp_path)
    payload = {
        "grounding": [
            {
                "kind": "decision",
                "text": "fabricated",
                "source_excerpt": "DECISION: nothing like this exists in the transcript",
                "start_line": 3,
                "end_line": 3,
            }
        ]
    }
    passed, reasons = evaluate_grounding(ti, payload)
    assert passed is False
    assert any("excerpt_not_in_transcript" in r for r in reasons)


def test_evaluate_grounding_fails_on_out_of_range_lines(tmp_path):
    ti = _setup(tmp_path)
    payload = {
        "grounding": [
            {
                "kind": "decision",
                "text": "x",
                "source_excerpt": ti.transcript_lines[0],
                "start_line": 999,
                "end_line": 999,
            }
        ]
    }
    passed, reasons = evaluate_grounding(ti, payload)
    assert passed is False
    assert any("start_line_out_of_range" in r for r in reasons)


def test_evaluate_grounding_passes_when_grounding_absent(tmp_path):
    ti = _setup(tmp_path)
    payload = {}
    passed, reasons = evaluate_grounding(ti, payload)
    assert passed is True


def test_excerpt_is_in_transcript(tmp_path):
    ti = _setup(tmp_path)
    assert excerpt_is_in_transcript(ti, "DECISION: Approve Q3 plan.") is True
    assert excerpt_is_in_transcript(ti, "DECISION: nope") is False


def test_pipeline_artifact_fails_grounding_eval_when_excerpt_absent(tmp_path):
    """Failing grounding eval blocks promotion through control."""
    ti = _setup(tmp_path)
    result = run_transcript_pipeline(
        lake_root=tmp_path,
        transcript_input=ti,
        workflow_name="meeting_minutes",
    )
    assert result.promoted is True
    assert result.grounding_eval.payload["status"] == "pass"

    # Now mutate one excerpt to break grounding without removing the eval
    result.target.payload["grounding"].append(
        {
            "kind": "decision",
            "text": "fabricated",
            "source_excerpt": "DECISION: This decision is not in the transcript at all",
            "start_line": 3,
            "end_line": 3,
        }
    )
    passed, reasons = evaluate_grounding(ti, result.target.payload)
    assert passed is False
    assert reasons
