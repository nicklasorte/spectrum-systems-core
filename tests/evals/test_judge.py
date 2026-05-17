"""Phase X2.3 — LLM-as-judge tests.

Defends the trust property that ``JUDGE_ENABLED=false`` produces zero
model calls AND that calibration failure halts the gate. Uses an
in-process api_caller mock so no network calls happen.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from spectrum_systems_core.evals.judge import (
    DEFAULT_JUDGE_MODEL,
    JUDGE_ENABLED_ENV,
    JUDGE_MODEL_ENV,
    JUDGE_STABILITY_CHECK_ENABLED_ENV,
    RUBRIC_CHECKS,
    build_judge_prompt,
    judge_score_to_artifact,
    parse_judge_response,
    run_judge,
)
from spectrum_systems_core.evals.judge_calibration import (
    calibrate,
    calibration_to_artifact,
)
from spectrum_systems_core.validation import validate_artifact

# ----- env-gating -------------------------------------------------


def test_judge_disabled_by_default_produces_zero_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JUDGE_ENABLED_ENV, raising=False)
    call_count = {"n": 0}

    def fake_caller(prompt: str) -> str:
        call_count["n"] += 1
        return "{}"

    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=fake_caller,
    )
    assert result.enabled is False
    assert call_count["n"] == 0
    assert result.items_evaluated == 0
    assert result.aggregate_pass_rate is None


def test_judge_enabled_uses_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    monkeypatch.delenv(JUDGE_MODEL_ENV, raising=False)
    result = run_judge(
        decisions=[],
        source_texts_by_chunk={},
        api_caller=lambda p: "{}",
    )
    assert result.enabled is True
    assert result.judge_model == DEFAULT_JUDGE_MODEL


# ----- prompt + parser --------------------------------------------


def test_prompt_includes_decision_and_source() -> None:
    prompt = build_judge_prompt(
        {"decision_text": "FCC approved", "decision_outcome": "approval"},
        "Chair: FCC has approved the rule.",
    )
    assert "EXTRACTED DECISION" in prompt
    assert "SOURCE CHUNK" in prompt
    for check in RUBRIC_CHECKS:
        assert check in prompt


def test_parse_extracts_all_four_booleans() -> None:
    resp = json.dumps({
        "decision_text_supported_by_source": True,
        "decision_outcome_matches_regulatory_verb": True,
        "speaker_attribution_correct": False,
        "no_hallucinated_constraints_or_actors": True,
    })
    parsed = parse_judge_response(resp)
    assert parsed["decision_text_supported_by_source"] is True
    assert parsed["speaker_attribution_correct"] is False
    assert all(parsed[k] is not None for k in RUBRIC_CHECKS)


def test_parse_missing_field_becomes_none() -> None:
    resp = json.dumps({"decision_text_supported_by_source": True})
    parsed = parse_judge_response(resp)
    assert parsed["decision_text_supported_by_source"] is True
    assert parsed["speaker_attribution_correct"] is None


def test_parse_garbage_returns_all_none() -> None:
    parsed = parse_judge_response("model said hello")
    assert all(v is None for v in parsed.values())


# ----- run ----------------------------------------------------------


def _all_true_response() -> str:
    return json.dumps({k: True for k in RUBRIC_CHECKS})


def _one_false_response(check: str) -> str:
    out = {k: True for k in RUBRIC_CHECKS}
    out[check] = False
    return json.dumps(out)


def test_run_judge_marks_all_true_as_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: _all_true_response(),
    )
    assert result.items_evaluated == 1
    assert result.item_scores[0].passed is True
    assert result.aggregate_pass_rate == 1.0


def test_run_judge_marks_any_false_as_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: _one_false_response("speaker_attribution_correct"),
    )
    assert result.item_scores[0].passed is False
    assert "speaker_attribution_correct=false" in result.item_scores[0].failure_reasons


def test_unparseable_response_marks_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: "unparseable",
    )
    assert result.item_scores[0].judge_decision == "unparseable"
    assert result.aggregate_pass_rate is None


def test_api_exception_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")

    def boom(prompt: str) -> str:
        raise RuntimeError("timeout")

    # Must not raise.
    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=boom,
    )
    assert result.items_evaluated == 1
    assert result.item_scores[0].judge_decision == "unparseable"


def test_same_family_warning_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "claude-haiku-4-5")
    result = run_judge(
        decisions=[],
        source_texts_by_chunk={},
        api_caller=lambda p: "{}",
        extraction_model="claude-haiku-4-5-20251001",
    )
    assert any(f.finding_code == "judge_same_family" for f in result.findings)


def test_different_family_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "claude-sonnet-4-6")
    result = run_judge(
        decisions=[],
        source_texts_by_chunk={},
        api_caller=lambda p: "{}",
        extraction_model="claude-haiku-4-5-20251001",
    )
    assert not any(f.finding_code == "judge_same_family" for f in result.findings)


def test_stability_check_emits_unstable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    monkeypatch.setenv(JUDGE_STABILITY_CHECK_ENABLED_ENV, "true")
    responses: list[str] = [
        _all_true_response(),
        _one_false_response("speaker_attribution_correct"),
    ]

    def alternating(prompt: str) -> str:
        return responses.pop(0)

    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=alternating,
    )
    assert result.item_scores[0].stability_match is False
    assert any(f.finding_code == "judge_score_unstable" for f in result.findings)


# ----- artifact validation ----------------------------------------


def test_judge_score_artifact_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = run_judge(
        decisions=[{"decision_id": "d1", "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: _all_true_response(),
    )
    artifact = judge_score_to_artifact(result, source_id="src", pipeline_run_id="run1")
    validate_artifact(artifact, "judge_score")


# ----- calibration ------------------------------------------------


def _judge_pass(decision_id: str = "d1") -> Any:
    """Build a JudgeRunResult-like object via run_judge."""
    return run_judge(
        decisions=[{"decision_id": decision_id, "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: _all_true_response(),
    )


def _judge_fail(decision_id: str = "d1") -> Any:
    return run_judge(
        decisions=[{"decision_id": decision_id, "decision_text": "x"}],
        source_texts_by_chunk={},
        api_caller=lambda p: _one_false_response("speaker_attribution_correct"),
    )


def test_calibration_unrun_when_no_decision_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = _judge_pass()
    record = calibrate(result, ground_truth_pairs=[])
    assert record.calibration_status == "unrun"
    assert record.agreement_rate_overall is None
    assert record.agreement_rate_verb_discrimination is None
    assert record.findings == []


def test_calibration_ok_when_agreement_above_warn_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = _judge_pass("d1")
    pairs = [
        {"pair_id": "p1", "target_type": "decision", "decision_id": "d1", "ground_truth_pass": True},
    ]
    record = calibrate(result, ground_truth_pairs=pairs)
    assert record.calibration_status == "ok"
    assert record.agreement_rate_overall == 1.0


def test_calibration_low_below_warn_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    # 10 items: 6 pass, 4 fail -- judge always passes => 0.6 agreement.
    decisions = [{"decision_id": f"d{i}", "decision_text": "x"} for i in range(10)]
    result = run_judge(
        decisions=decisions,
        source_texts_by_chunk={},
        api_caller=lambda p: _all_true_response(),
    )
    pairs: list[dict[str, Any]] = []
    for i in range(10):
        pairs.append({
            "pair_id": f"p{i}",
            "target_type": "decision",
            "decision_id": f"d{i}",
            "ground_truth_pass": i < 7,  # 7 pass, 3 fail -> agreement = 0.7
        })
    record = calibrate(result, ground_truth_pairs=pairs)
    # judge passes everything, GT passes 7/10 -> agreement 0.7 -> ok
    assert record.calibration_status == "ok"

    # Now flip GT to half pass, half fail (5/10 = 0.5) -> halt
    pairs2 = [
        {"pair_id": f"p{i}", "target_type": "decision",
         "decision_id": f"d{i}", "ground_truth_pass": i < 5}
        for i in range(10)
    ]
    record2 = calibrate(result, ground_truth_pairs=pairs2)
    assert record2.agreement_rate_overall == 0.5
    assert record2.calibration_status == "failed"
    assert any(f.finding_code == "judge_calibration_failed" for f in record2.findings)


def test_calibration_warn_band(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    decisions = [{"decision_id": f"d{i}", "decision_text": "x"} for i in range(10)]
    result = run_judge(
        decisions=decisions,
        source_texts_by_chunk={},
        api_caller=lambda p: _all_true_response(),
    )
    pairs = [
        {"pair_id": f"p{i}", "target_type": "decision",
         "decision_id": f"d{i}", "ground_truth_pass": i < 6}
        for i in range(10)
    ]
    record = calibrate(result, ground_truth_pairs=pairs)
    # agreement = 0.6 (>= halt, < warn) -> warn band
    assert record.agreement_rate_overall == pytest.approx(0.6)
    assert record.calibration_status == "warn"
    assert any(f.finding_code == "judge_calibration_low" for f in record.findings)


def test_verb_discrimination_subset_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = _judge_pass("d1")
    pairs = [
        {
            "pair_id": "p1", "target_type": "decision",
            "decision_id": "d1", "ground_truth_pass": True,
            "rubric_notes": {"verb_discrimination_example": True},
        },
    ]
    record = calibrate(result, ground_truth_pairs=pairs)
    assert record.agreement_rate_verb_discrimination == 1.0
    assert record.verb_discrimination_pairs == 1


def test_verb_discrimination_none_when_no_annotated_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = _judge_pass("d1")
    pairs = [
        {"pair_id": "p1", "target_type": "decision",
         "decision_id": "d1", "ground_truth_pass": True},
    ]
    record = calibrate(result, ground_truth_pairs=pairs)
    assert record.agreement_rate_verb_discrimination is None
    assert record.verb_discrimination_pairs == 0


def test_calibration_artifact_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_ENABLED_ENV, "true")
    result = _judge_pass("d1")
    record = calibrate(result, ground_truth_pairs=[])
    artifact = calibration_to_artifact(record)
    validate_artifact(artifact, "judge_calibration_record")
