"""Phase P2: tests for prompt_version, calibration_data, and judge metrics.

These tests defend three trust properties added before scaling to 13
transcripts:

* ``prompt_version`` is deterministic: the same prompt template always
  hashes to the same string, a one-character edit hashes differently.
* ``calibration_data`` carries one entry per extracted decision, with
  ``aligned`` set correctly even for spurious decisions whose outcome
  matches a GT bucket but whose text overlap fails.
* The ``meeting_extraction`` schema still validates artifacts written
  before Phase P2-B (no ``prompt_version`` field).

The tests do NOT exercise the live LLM path; the prompt-version helper
is a pure function over a string and the calibration helper is a pure
function over decision + GT-pair dicts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.evals.alignment import compute_alignment
from spectrum_systems_core.extraction._prompt_blocks import (
    PROMPT_VERSION,
    _canonical_prompt_template,
    compute_prompt_version,
)
from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger
from spectrum_systems_core.validation import validate_artifact


# -- B: prompt_version -------------------------------------------------


def test_prompt_version_stable_across_runs() -> None:
    """Same prompt template => same prompt_version, always."""
    template = _canonical_prompt_template()
    v1 = compute_prompt_version(template)
    v2 = compute_prompt_version(template)
    assert v1 == v2
    assert v1 == PROMPT_VERSION


def test_prompt_version_changes_on_prompt_change() -> None:
    """One-byte edit to the template => different prompt_version."""
    template = _canonical_prompt_template()
    v_orig = compute_prompt_version(template)
    v_edited = compute_prompt_version(template + "X")
    assert v_orig != v_edited
    v_edited_again = compute_prompt_version(template + "X")
    assert v_edited == v_edited_again


def test_prompt_version_format_sha256_prefix() -> None:
    """Format contract: 'sha256:<12 hex chars>'."""
    import re

    assert re.fullmatch(r"sha256:[0-9a-f]{12}", PROMPT_VERSION)


def test_merger_stamps_prompt_version() -> None:
    """ExtractionMerger.merge stamps the prompt_version field."""
    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000001",
        extraction_run_id="rr",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
    )
    assert artifact["prompt_version"] == PROMPT_VERSION


def test_schema_validates_without_prompt_version() -> None:
    """meeting_extraction artifacts without prompt_version still validate.

    Required for Phase P2-B rollout: old artifacts already on disk
    predate this field and must not be retroactively invalidated.
    """
    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000002",
        extraction_run_id="rr",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
    )
    artifact.pop("prompt_version")
    validate_artifact(artifact, "meeting_extraction")


def test_schema_rejects_malformed_prompt_version() -> None:
    """A non-sha256-format prompt_version is rejected by the schema."""
    artifact = ExtractionMerger().merge(
        source_artifact_id="00000000-0000-0000-0000-000000000003",
        extraction_run_id="rr",
        classifications=[],
        decisions=[],
        claims=[],
        action_items=[],
    )
    artifact["prompt_version"] = "garbage-not-sha256"
    with pytest.raises(Exception):
        validate_artifact(artifact, "meeting_extraction")


# -- C: calibration_data ----------------------------------------------


def test_calibration_data_populated() -> None:
    """3 decisions, 3 GT pairs => 3 calibration_data entries with conf."""
    extracted = [
        {
            "decision_text": "Approved itu two-point criteria",
            "decision_outcome": "approval",
            "confidence": 0.9,
        },
        {
            "decision_text": "Group deferred 80 percent threshold",
            "decision_outcome": "deferral",
            "confidence": 0.6,
        },
        {
            "decision_text": "Action required: post recordings",
            "decision_outcome": "action_required",
            "confidence": 0.3,
        },
    ]
    gt = [
        {
            "pair_id": "p1",
            "ground_truth_text": "Approved itu two-point criteria for FSS",
            "expected_decision_outcome": "approval",
        },
        {
            "pair_id": "p2",
            "ground_truth_text": "Group deferred the 80 percent threshold",
            "expected_decision_outcome": "deferral",
        },
        {
            "pair_id": "p3",
            "ground_truth_text": "Owners must post the recordings",
            "expected_decision_outcome": "action_required",
        },
    ]
    result = compute_alignment(extracted_decisions=extracted, gt_pairs=gt)
    calibration = result["calibration_data"]
    assert len(calibration) == 3
    confidences = [c["confidence"] for c in calibration]
    assert confidences == [0.9, 0.6, 0.3]
    # All three should be aligned (text overlap + outcome match).
    assert all(c["aligned"] is True for c in calibration), calibration


def test_calibration_data_flags_spurious_as_aligned_false() -> None:
    """A decision matching an outcome bucket but failing text overlap
    must appear in calibration_data with aligned=false (not silently
    dropped). That's the seed signal P3-A will rely on.
    """
    extracted = [
        {
            "decision_text": "Approved itu two-point criteria",
            "decision_outcome": "approval",
            "confidence": 0.9,
        },
        # Spurious: outcome matches but text is unrelated noise.
        {
            "decision_text": "totally unrelated jabberwocky widget",
            "decision_outcome": "approval",
            "confidence": 0.55,
        },
    ]
    gt = [
        {
            "pair_id": "p1",
            "ground_truth_text": "Approved itu two-point criteria for FSS",
            "expected_decision_outcome": "approval",
        },
    ]
    result = compute_alignment(extracted_decisions=extracted, gt_pairs=gt)
    cal = result["calibration_data"]
    assert len(cal) == 2
    assert cal[0]["aligned"] is True
    assert cal[1]["aligned"] is False
    assert cal[1]["confidence"] == 0.55
    assert cal[1]["outcome"] == "approval"


def test_calibration_note_signals_ece_readiness() -> None:
    """The note must distinguish <30 (seed mode) from >=30 (ECE mode)."""
    few = compute_alignment(
        extracted_decisions=[
            {"decision_text": "x", "decision_outcome": "approval", "confidence": 0.9}
        ],
        gt_pairs=[],
    )
    assert "insufficient for ECE" in (few["calibration_note"] or "")

    many = compute_alignment(
        extracted_decisions=[
            {
                "decision_text": f"d-{i}",
                "decision_outcome": "approval",
                "confidence": 0.9,
            }
            for i in range(30)
        ],
        gt_pairs=[],
    )
    assert "ECE computable" in (many["calibration_note"] or "")


def test_calibration_data_handles_missing_confidence() -> None:
    """A decision without a numeric confidence => confidence=None entry."""
    extracted = [
        {"decision_text": "x", "decision_outcome": "approval"},
        {"decision_text": "y", "decision_outcome": "approval", "confidence": "high"},
    ]
    result = compute_alignment(extracted_decisions=extracted, gt_pairs=[])
    cal = result["calibration_data"]
    assert cal[0]["confidence"] is None
    assert cal[1]["confidence"] is None


# -- D: judge calibration ---------------------------------------------


def test_judge_calibration_agreement_rate(tmp_path: Path) -> None:
    """Given a known judge_score artifact and matching GT pairs, the
    runner computes the agreement rate correctly.

    The runner reads judge_score artifacts from <sdl_root>/judge_scores/
    and joins them to evaluated_pairs by ``decision_id``. We exercise
    that join directly via a stub _resolve_pair_source_id.
    """
    from spectrum_systems_core.evals.m4.runner import EvalRunner

    sdl_root = tmp_path / "store" / "artifacts"
    sdl_root.mkdir(parents=True)
    (sdl_root / "judge_scores").mkdir()
    judge_doc = {
        "artifact_type": "judge_score",
        "schema_version": "1.0.0",
        "judge_score_id": "js-1",
        "judge_run_id": "jr-1",
        "source_id": "src-a",
        "judge_model": "claude-sonnet-4-6",
        "enabled": True,
        "items_evaluated": 3,
        "aggregate_pass_rate": 2 / 3,
        "calibration_status": "ok",
        "item_scores": [
            {
                "item_id": "d-1",
                "decision_text": "d-1 text",
                "rubric_results": {},
                "passed": True,
                "failure_reasons": [],
                "judge_decision": "pass",
            },
            {
                "item_id": "d-2",
                "decision_text": "d-2 text",
                "rubric_results": {},
                "passed": False,
                "failure_reasons": ["x=false"],
                "judge_decision": "fail",
            },
            {
                "item_id": "d-3",
                "decision_text": "d-3 text",
                "rubric_results": {},
                "passed": True,
                "failure_reasons": [],
                "judge_decision": "pass",
            },
        ],
        "provenance": {"produced_by": "JudgeRunner"},
    }
    (sdl_root / "judge_scores" / "js-1.json").write_text(
        json.dumps(judge_doc), encoding="utf-8"
    )

    runner = EvalRunner(
        data_lake_path=str(tmp_path), sdl_root=str(sdl_root),
    )
    # Three pairs: d-1 agree (both pass), d-2 agree (both fail),
    # d-3 disagree (judge passes, human says fail).
    evaluated_pairs = [
        {
            "pair_id": "p1",
            "target_type": "decision",
            "decision_id": "d-1",
            "ground_truth_pass": True,
            "source_artifact_id": "src-a",
        },
        {
            "pair_id": "p2",
            "target_type": "decision",
            "decision_id": "d-2",
            "ground_truth_pass": False,
            "source_artifact_id": "src-a",
        },
        {
            "pair_id": "p3",
            "target_type": "decision",
            "decision_id": "d-3",
            "ground_truth_pass": False,
            "source_artifact_id": "src-a",
        },
    ]
    # Patch resolver to honour the source_artifact_id field.
    runner._resolve_pair_source_id = lambda p: p.get("source_artifact_id")

    metrics = runner._compute_judge_metrics(evaluated_pairs)
    assert metrics["judge_evaluated_count"] == 3
    assert metrics["judge_pass_rate"] == pytest.approx(2 / 3)
    assert metrics["judge_human_agreement_rate"] == pytest.approx(2 / 3)


def test_judge_metrics_absent_when_no_artifact(tmp_path: Path) -> None:
    """No judge_score artifact => everything null with explanatory note."""
    from spectrum_systems_core.evals.m4.runner import EvalRunner

    sdl_root = tmp_path / "store" / "artifacts"
    sdl_root.mkdir(parents=True)

    runner = EvalRunner(data_lake_path=str(tmp_path), sdl_root=str(sdl_root))
    runner._resolve_pair_source_id = lambda p: p.get("source_artifact_id")

    pairs = [
        {
            "pair_id": "p1",
            "target_type": "decision",
            "decision_id": "d-1",
            "ground_truth_pass": True,
            "source_artifact_id": "src-a",
        }
    ]
    metrics = runner._compute_judge_metrics(pairs)
    assert metrics["judge_evaluated_count"] == 0
    assert metrics["judge_pass_rate"] is None
    assert metrics["judge_human_agreement_rate"] is None
    assert "no judge_score artifact" in metrics["judge_calibration_note"]
