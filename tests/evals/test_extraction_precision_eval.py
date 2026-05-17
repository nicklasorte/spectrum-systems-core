"""Tests for the Phase Z.3 extraction precision eval."""
from __future__ import annotations

import json
from pathlib import Path

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals import (
    EXTRACTION_PRECISION_EVAL_TYPE,
    LCS_THRESHOLD,
    SOURCE_RECORD_MISSING,
    SOURCE_TEXT_NOT_GROUNDED_PREFIX,
    TURN_ID_NOT_FOUND_PREFIX,
    run_extraction_precision_eval,
)
from spectrum_systems_core.evals.extraction_precision import (
    EMPTY_SOURCE_TURNS_PREFIX,
)

# ---- fixtures ------------------------------------------------------------


def _write_source_record(
    tmp_path: Path, chunks: list[dict]
) -> Path:
    record = {
        "artifact_id": "src-test",
        "artifact_type": "source_record",
        "schema_version": "1.0.0",
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "trace-test",
        "input_refs": [],
        "content_hash": "x",
        "payload": {
            "meeting_id": "m-test",
            "transcript_hash": "h",
            "chunks": chunks,
        },
    }
    path = tmp_path / "source_record__m-test.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _meeting_minutes(decisions: list[dict]):
    payload = {
        "title": "x",
        "summary": "x",
        "decisions": decisions,
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.1.0",
    }
    return new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )


# ---- happy paths --------------------------------------------------------


def test_exact_substring_match_passes(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001",
         "text": "The FCC approved the SAS-only sharing framework for Phase 1."},
    ])
    artifact = _meeting_minutes([
        {"text": "FCC approved the SAS-only sharing framework",
         "verb": "approved",
         "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["eval_type"] == EXTRACTION_PRECISION_EVAL_TYPE
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_paraphrase_match_within_threshold_passes(tmp_path):
    # Tight paraphrase — LCS ratio over 0.7 with the original turn.
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001",
         "text": "NTIA deferred the technical specification review."},
    ])
    artifact = _meeting_minutes([
        {"text": "NTIA deferred the technical specification.",
         "verb": "deferred",
         "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "pass"


def test_artifact_with_no_source_turns_fields_passes(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "x"},
    ])
    payload = {
        "title": "x", "summary": "x",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.1.0",
    }
    artifact = new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


# ---- rejection paths ----------------------------------------------------


def test_zero_overlap_blocks_with_specific_finding(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "The FCC approved the framework."},
    ])
    artifact = _meeting_minutes([
        {"text": "Hovercrafts full of eels arrived.",
         "verb": "noted",
         "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(SOURCE_TEXT_NOT_GROUNDED_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_zero_overlap_routes_to_block_through_decide_control(tmp_path):
    """Fail-closed: an ungrounded item must trigger ``block`` from
    ``decide_control``, not a silent pass."""
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "The FCC approved the framework."},
    ])
    artifact = _meeting_minutes([
        {"text": "Hovercrafts full of eels.",
         "verb": "noted",
         "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    decision = decide_control(artifact, [result])
    assert decision.payload["decision"] == "block"
    assert any(
        f"failed:{EXTRACTION_PRECISION_EVAL_TYPE}" in r
        for r in decision.payload["reason_codes"]
    )


def test_unknown_turn_id_blocks(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "x"},
    ])
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t9999"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{TURN_ID_NOT_FOUND_PREFIX}t9999")
        for r in result.payload["reason_codes"]
    )


def test_source_record_missing_blocks(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, missing)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(SOURCE_RECORD_MISSING)
        for r in result.payload["reason_codes"]
    )


def test_empty_source_record_path_string_blocks():
    """A wiring regression — an empty string path — must not be
    interpreted as 'nothing to verify'."""
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, "")
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(SOURCE_RECORD_MISSING)
        for r in result.payload["reason_codes"]
    )


def test_none_source_record_path_blocks():
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, None)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(SOURCE_RECORD_MISSING)
        for r in result.payload["reason_codes"]
    )


def test_mixed_grounded_and_ungrounded_blocks_on_ungrounded(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "The FCC approved the framework."},
        {"turn_id": "t0002", "text": "Alice ran into the room."},
    ])
    artifact = _meeting_minutes([
        {"text": "FCC approved the framework",
         "verb": "approved", "source_turns": ["t0001"]},
        {"text": "Hovercrafts of eels.",
         "verb": "deferred", "source_turns": ["t0002"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"
    # The block must be specifically about the ungrounded item, not
    # the grounded one.
    failure_codes = [
        r for r in result.payload["reason_codes"]
        if r.startswith(SOURCE_TEXT_NOT_GROUNDED_PREFIX)
    ]
    assert failure_codes, result.payload["reason_codes"]


def test_empty_source_turns_with_item_text_blocks(tmp_path):
    """A regression test — an extractor could otherwise pass the gate
    by emitting ``source_turns: []`` for every item with text."""
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "x"},
    ])
    artifact = _meeting_minutes([
        {"text": "The FCC approved something.", "verb": "approved",
         "source_turns": []},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(EMPTY_SOURCE_TURNS_PREFIX)
        for r in result.payload["reason_codes"]
    )


# ---- LCS threshold adversarial -----------------------------------------


def test_lcs_ratio_below_threshold_blocks(tmp_path):
    """An adversarial item whose LCS ratio is 0.69 — just below the 0.7
    threshold — must block. This pins the threshold so a future
    "just relax it a little" tweak gets caught."""
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001",
         "text": "The FCC approved the new spectrum framework today."},
    ])
    # Construct an item text whose LCS ratio against the turn is < 0.7.
    item_text = "Hovercrafts arrived shortly after lunch yesterday."
    artifact = _meeting_minutes([
        {"text": item_text, "verb": "approved",
         "source_turns": ["t0001"]},
    ])
    # Sanity-check the math: this string really is below the threshold.
    import difflib
    ratio = difflib.SequenceMatcher(
        None, item_text.lower(),
        "The FCC approved the new spectrum framework today.".lower(),
    ).ratio()
    assert ratio < LCS_THRESHOLD, (
        f"adversarial ratio {ratio} unexpectedly >= threshold "
        f"{LCS_THRESHOLD} — pick a different adversarial text"
    )
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"


def test_lcs_threshold_constant_pinned_at_0_7():
    """Adversarial threshold test: relaxing this is a trust regression.
    Use a separate PR (and a judgment record) if it ever needs to
    change."""
    assert LCS_THRESHOLD == 0.7


def test_short_turn_long_paraphrase_does_not_falsely_pass(tmp_path):
    """A 30-word paraphrase of a 2-word turn should fail. SequenceMatcher
    on very different-length strings yields a low ratio, but this test
    pins the behaviour so a future "boost short-turn scores" change
    cannot silently weaken the gate."""
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001", "text": "Hello there."},
    ])
    long_paraphrase = " ".join(["spectrum"] * 30)
    artifact = _meeting_minutes([
        {"text": long_paraphrase, "verb": "approved",
         "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, sr)
    assert result.payload["status"] == "fail"


# ---- determinism --------------------------------------------------------


def test_eval_is_deterministic_across_repeated_runs(tmp_path):
    sr = _write_source_record(tmp_path, [
        {"turn_id": "t0001",
         "text": "The FCC approved the SAS-only sharing framework."},
        {"turn_id": "t0002",
         "text": "NTIA deferred the technical review."},
    ])
    artifact = _meeting_minutes([
        {"text": "FCC approved the SAS-only sharing framework",
         "verb": "approved", "source_turns": ["t0001"]},
        {"text": "NTIA deferred the review",
         "verb": "deferred", "source_turns": ["t0002"]},
    ])
    results = [
        run_extraction_precision_eval(artifact, sr).payload
        for _ in range(3)
    ]
    # status + reason_codes must be byte-identical across runs.
    assert results[0]["status"] == results[1]["status"] == results[2]["status"]
    assert (
        results[0]["reason_codes"]
        == results[1]["reason_codes"]
        == results[2]["reason_codes"]
    )


# ---- malformed source_record paths -------------------------------------


def test_invalid_json_source_record_blocks(tmp_path):
    bad = tmp_path / "source_record__m-test.json"
    bad.write_text("not json", encoding="utf-8")
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, bad)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(SOURCE_RECORD_MISSING)
        for r in result.payload["reason_codes"]
    )


def test_source_record_without_chunks_blocks(tmp_path):
    record_path = tmp_path / "source_record__m-test.json"
    record_path.write_text(
        json.dumps({"payload": {"chunks": []}}), encoding="utf-8"
    )
    artifact = _meeting_minutes([
        {"text": "x", "verb": "approved", "source_turns": ["t0001"]},
    ])
    result = run_extraction_precision_eval(artifact, record_path)
    assert result.payload["status"] == "fail"
