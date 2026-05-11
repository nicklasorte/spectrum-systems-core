"""Tests for RegressionGate (Phase M.4)."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.evals.m4 import EvalRunner, RegressionGate

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "eval"


def _load_schema(name: str) -> dict:
    return json.loads(
        (CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def _summary(eval_summary_id: str = "11111111-aaaa-4aaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {
        "eval_summary_id": eval_summary_id,
        "pipeline_run_id": "run-1",
        "artifact_type": "eval_summary",
        "schema_version": "1.0.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "pairs_evaluated": 0,
        "pairs_skipped_pending_review": 0,
        "aggregate_coverage": 0.0,
        "aggregate_precision": 0.0,
        "total_items_requiring_review": 0,
        "by_chunking_strategy": {
            "speaker_turn": {"coverage": 0.0, "precision": 0.0, "pairs_count": 0},
            "character_count_fallback": {
                "coverage": 0.0,
                "precision": 0.0,
                "pairs_count": 0,
            },
        },
        "eval_results": [],
        "is_baseline": False,
        "baseline_eval_summary_id": None,
        "regression_detected": False,
        "regression_detail": [],
        "provenance": {"produced_by": "EvalRunner"},
    }


def _pair_result(
    pair_id: str, *, coverage: float = 0.8, review_rate: float = 0.1
) -> dict:
    return {
        "pair_id": pair_id,
        "coverage": coverage,
        "items_requiring_review_rate": review_rate,
    }


def test_first_run_writes_baseline_no_gate(tmp_path) -> None:
    """run_count = 1, no baseline -> skip_no_baseline + baseline written."""
    gate = RegressionGate()
    result = gate.evaluate(
        current_summary=_summary(),
        baseline_summary=None,
        run_count=1,
    )
    assert result["decision"] == "skip_no_baseline"
    assert result["regression_detail"] == []

    # Now exercise the actual disk-write side via the runner.
    sdl_root = tmp_path / "sdl"
    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-1",
    )
    out = runner.run()
    assert out["status"] == "completed"
    baseline_path = sdl_root / "evals" / "baseline_eval_summary.json"
    assert baseline_path.is_file(), (
        "First run must write the baseline_eval_summary.json so subsequent "
        "runs have something to compare against."
    )


def test_second_run_skips_gate() -> None:
    """run_count = 2 -> skip_run_count even with baseline present."""
    gate = RegressionGate()
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=2,
    )
    assert result["decision"] == "skip_run_count"


def test_third_run_enforces_gate_on_coverage_drop() -> None:
    """run_count = 3, coverage drop 0.20 > 0.15 threshold -> block."""
    gate = RegressionGate()
    current = [_pair_result("p1", coverage=0.60, review_rate=0.1)]
    baseline = [_pair_result("p1", coverage=0.80, review_rate=0.1)]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    assert result["decision"] == "block"
    assert any(
        r["pair_id"] == "p1" and r["metric"] == "coverage"
        for r in result["regression_detail"]
    )


def test_gate_allows_when_within_threshold() -> None:
    """run_count = 3, coverage drop 0.08 < 0.15 -> allow.

    This is the paired happy-path test for test_third_run_enforces_gate_on_coverage_drop
    -- proves the SAME code path returns allow when within thresholds.
    """
    gate = RegressionGate()
    current = [_pair_result("p1", coverage=0.72, review_rate=0.1)]
    baseline = [_pair_result("p1", coverage=0.80, review_rate=0.1)]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    assert result["decision"] == "allow"
    assert result["regression_detail"] == []


def test_third_run_enforces_gate_on_review_rate_rise() -> None:
    """run_count = 3, review_rate rise 0.25 > 0.20 -> block."""
    gate = RegressionGate()
    current = [_pair_result("p1", coverage=0.85, review_rate=0.40)]
    baseline = [_pair_result("p1", coverage=0.85, review_rate=0.15)]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    assert result["decision"] == "block"
    assert any(
        r["pair_id"] == "p1" and r["metric"] == "items_requiring_review_rate"
        for r in result["regression_detail"]
    )


def test_gate_allows_when_review_rate_within_threshold() -> None:
    """Happy-path counterpart to the review_rate rise block test."""
    gate = RegressionGate()
    current = [_pair_result("p1", coverage=0.85, review_rate=0.20)]
    baseline = [_pair_result("p1", coverage=0.85, review_rate=0.15)]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    assert result["decision"] == "allow"


def test_regression_detail_lists_affected_pairs() -> None:
    """Two pairs regress, one does not -> regression_detail has exactly the
    two regressing pairs."""
    gate = RegressionGate()
    current = [
        _pair_result("p1", coverage=0.50, review_rate=0.10),
        _pair_result("p2", coverage=0.85, review_rate=0.10),
        _pair_result("p3", coverage=0.30, review_rate=0.10),
    ]
    baseline = [
        _pair_result("p1", coverage=0.80, review_rate=0.10),
        _pair_result("p2", coverage=0.85, review_rate=0.10),
        _pair_result("p3", coverage=0.80, review_rate=0.10),
    ]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    assert result["decision"] == "block"
    affected = {r["pair_id"] for r in result["regression_detail"]}
    assert affected == {"p1", "p3"}, (
        "Only the two regressing pairs (p1, p3) must appear; p2 is "
        "within threshold and must NOT be listed."
    )


def test_gate_decision_schema_validates() -> None:
    """Gate output validates against the gate_decision schema."""
    schema = _load_schema("gate_decision")
    gate = RegressionGate()
    decision = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=[
            _pair_result("p1", coverage=0.50, review_rate=0.10)
        ],
        baseline_pair_results=[
            _pair_result("p1", coverage=0.80, review_rate=0.10)
        ],
    )
    jsonschema.Draft202012Validator(schema).validate(decision)
    assert decision["artifact_type"] == "gate_decision"


def test_empty_baseline_pair_results_blocks_not_allows() -> None:
    """RT1 fix: gate active + baseline summary present + 0 baseline pair
    records + current has pairs -> block (not allow).

    The earlier code emitted decision=allow with reason=within_thresholds
    because the per-pair loop found no overlapping pair_ids and so
    produced an empty regression_detail. That silently let any run pass
    whenever the baseline_eval_summary's eval_results list was empty
    (zero confirmed pairs at baseline time, or its eval_result files
    missing on disk). Fail-closed instead.
    """
    gate = RegressionGate()
    current = [_pair_result("p1", coverage=0.30, review_rate=0.50)]
    decision = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=[],
    )
    assert decision["decision"] == "block"
    assert decision["reason"] == "baseline_has_no_pair_results"


def test_empty_current_and_empty_baseline_is_allow() -> None:
    """Counterpart: when BOTH sides have no per-pair records, that's a
    benign state (a run with zero confirmed pairs) -- no regression
    can be diagnosed. Allow rather than block."""
    gate = RegressionGate()
    decision = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=[],
        baseline_pair_results=[],
    )
    assert decision["decision"] == "allow"


def test_runner_skips_pair_when_alignment_fails_validation(tmp_path) -> None:
    """RT1 fix: if the alignment_result fails schema validation, the
    eval_result MUST NOT be computed from it. The pair is excluded
    from the summary aggregation.

    We simulate the failure by injecting an aligner that returns an
    obviously-invalid alignment_result (artifact_type wrong).
    """
    from spectrum_systems_core.evals.m4 import EvalAligner, EvalMetrics

    class BadAligner(EvalAligner):
        def align(self, **kwargs):  # type: ignore[override]
            result = super().align(**kwargs)
            # Break the schema by setting an illegal artifact_type.
            result["artifact_type"] = "not_an_alignment_result"
            return result

    sdl_root = tmp_path / "sdl"
    # Stage one fixture pair.
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "eval"
        / "ground_truth"
        / "pair_001_confirmed_speaker_turn.json"
    )
    target_dir = sdl_root / "ground_truth"
    target_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(fixture_path, target_dir / fixture_path.name)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-bad-alignment",
        aligner=BadAligner(),
        metrics=EvalMetrics(),
    )
    result = runner.run()
    assert result["status"] == "completed"
    # The pair was skipped because its alignment_result was invalid.
    assert result["pairs_evaluated"] == 0
    assert result["summary"]["pairs_evaluated"] == 0
    # The invalid alignment_result was written as .invalid.json for
    # human inspection rather than dropped silently.
    invalid_files = list(
        (sdl_root / "evals" / "alignment").glob("*.invalid.json")
    )
    assert len(invalid_files) == 1


def test_summary_regression_detected_reflects_gate_verdict(tmp_path) -> None:
    """RT1 fix: eval_summary.regression_detected must match
    gate_decision.decision == block. Previously the summary always said
    False even when the gate said block.
    """
    sdl_root = tmp_path / "sdl"
    runner1 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-baseline",
    )
    runner1.run()

    # Force the gate into the "block" branch on run 3: empty baseline
    # pair_results + non-empty current. Stage one fixture pair so
    # current is non-empty.
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "eval"
        / "ground_truth"
        / "pair_001_confirmed_speaker_turn.json"
    )
    target_dir = sdl_root / "ground_truth"
    target_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(fixture_path, target_dir / fixture_path.name)

    # Wipe baseline eval_result files so baseline_pair_results loads as [].
    results_dir = sdl_root / "evals" / "results"
    for f in results_dir.glob("*.json"):
        f.unlink()

    runner2 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-2",
    )
    runner2.run()
    runner3 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-3",
    )
    out3 = runner3.run()
    assert out3["gate_decision"]["decision"] == "block"
    summary_path = (
        sdl_root / "evals" / f"eval_summary_{out3['pipeline_run_id']}.json"
    )
    on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
    assert on_disk["regression_detected"] is True, (
        "eval_summary on disk must agree with the gate verdict; "
        "anything else is a 'cannot be explained from the artifact "
        "alone' failure."
    )


def test_new_pair_cannot_regress() -> None:
    """A pair present only in current run -> cannot regress (no baseline value)."""
    gate = RegressionGate()
    current = [_pair_result("new-pair", coverage=0.30, review_rate=0.10)]
    baseline = [_pair_result("old-pair", coverage=0.90, review_rate=0.05)]
    result = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("base"),
        run_count=3,
        current_pair_results=current,
        baseline_pair_results=baseline,
    )
    # No matching pair_id -> no regression.
    assert result["decision"] == "allow"
    assert result["regression_detail"] == []


def test_gate_decision_baseline_id_recorded_when_baseline_present() -> None:
    """The baseline_eval_summary_id field must trace which baseline was used."""
    gate = RegressionGate()
    decision = gate.evaluate(
        current_summary=_summary("cur"),
        baseline_summary=_summary("baseline-xyz"),
        run_count=3,
        current_pair_results=[],
        baseline_pair_results=[],
    )
    assert decision["baseline_eval_summary_id"] == "baseline-xyz"


def test_gate_writes_baseline_via_runner_on_first_run_only(tmp_path) -> None:
    """End-to-end: first run installs baseline, second run does NOT overwrite."""
    sdl_root = tmp_path / "sdl"
    runner1 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-1",
    )
    runner1.run()
    baseline_path = sdl_root / "evals" / "baseline_eval_summary.json"
    assert baseline_path.is_file()
    first_mtime = baseline_path.stat().st_mtime

    # Run 2 -- no --set-baseline flag, must NOT overwrite.
    import time
    time.sleep(0.01)
    runner2 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-2",
    )
    runner2.run()
    second_mtime = baseline_path.stat().st_mtime
    assert second_mtime == first_mtime, (
        "Run 2 must NOT silently overwrite the baseline; only run 1 and "
        "explicit --set-baseline are allowed to install one."
    )


def test_dry_run_skipped_no_artifacts_written(tmp_path) -> None:
    """Dry run -> skipped, no eval artifacts on disk.

    This proves the no-artifacts side, not just a log line.
    """
    sdl_root = tmp_path / "sdl"
    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="rrun-dry",
    )
    result = runner.run(is_dry_run=True)
    assert result["status"] == "skipped"
    assert result["reason"] == "dry_run_skipped"
    # NO eval artifacts on disk -- this is the trust property.
    evals_dir = sdl_root / "evals"
    if evals_dir.exists():
        files = list(evals_dir.rglob("*.json"))
        assert files == [], (
            "Dry run must not write any eval artifacts; found: " + str(files)
        )
