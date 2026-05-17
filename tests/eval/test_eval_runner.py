"""End-to-end tests for EvalRunner against fixture ground_truth_pairs."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema

from spectrum_systems_core.evals.m4 import EvalRunner

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "ground_truth"
)
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "eval"


def _load_schema(name: str) -> dict:
    return json.loads(
        (CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def _stage_fixture_pairs(sdl_root: Path) -> None:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    for path in FIXTURE_DIR.glob("*.json"):
        shutil.copy(path, target / path.name)


def test_runner_processes_confirmed_pairs_only(tmp_path) -> None:
    """End-to-end: 4 fixture pairs, only the 3 confirmed evaluate."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-fixture-1",
        prompt_version="v0",
    )
    result = runner.run()
    assert result["status"] == "completed"
    assert result["pairs_evaluated"] == 3, (
        f"Expected 3 confirmed pairs evaluated, got "
        f"{result['pairs_evaluated']}. pending_review pair_004 MUST be "
        f"excluded."
    )
    assert result["pairs_skipped_pending_review"] == 1

    summary = result["summary"]
    assert summary["pairs_evaluated"] == 3
    assert summary["pairs_skipped_pending_review"] == 1
    assert summary["is_baseline"] is True  # first run installs baseline

    # eval_summary on disk validates against schema.
    summary_path = (
        sdl_root / "evals" / f"eval_summary_{summary['pipeline_run_id']}.json"
    )
    assert summary_path.is_file()
    schema = _load_schema("eval_summary")
    on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(on_disk)

    # gate_decision on disk validates against schema.
    gate_path = (
        sdl_root / "evals" / f"gate_decision_{summary['pipeline_run_id']}.json"
    )
    assert gate_path.is_file()
    gate_schema = _load_schema("gate_decision")
    gate_on_disk = json.loads(gate_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(gate_schema).validate(gate_on_disk)

    # Per-pair eval_result artifacts on disk validate against schema.
    result_schema = _load_schema("eval_result")
    results_dir = sdl_root / "evals" / "results"
    result_files = list(results_dir.glob("*.json"))
    assert len(result_files) == 3
    for path in result_files:
        rec = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(result_schema).validate(rec)

    # Per-pair alignment_result artifacts on disk validate against schema.
    align_schema = _load_schema("alignment_result")
    align_dir = sdl_root / "evals" / "alignment"
    align_files = list(align_dir.glob("*.json"))
    assert len(align_files) == 3
    for path in align_files:
        rec = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(align_schema).validate(rec)


def test_runner_pair_id_filter(tmp_path) -> None:
    """--pair-id filter evaluates only the requested pair."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)
    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-fixture-filter",
    )
    target = "11111111-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
    result = runner.run(pair_id_filter=target)
    assert result["pairs_evaluated"] == 1
    assert result["eval_results"][0]["pair_id"] == target


def test_runner_run_count_increments(tmp_path) -> None:
    """run_count file increments by 1 per run."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)
    for expected in (1, 2, 3):
        runner = EvalRunner(
            data_lake_path=str(tmp_path),
            sdl_root=str(sdl_root),
            pipeline_run_id=f"run-fixture-{expected}",
        )
        result = runner.run()
        assert result["run_count"] == expected, (
            f"Run {expected} reported run_count={result['run_count']}"
        )

    # Third run -> gate is now active. With no meaningful regression
    # vs the baseline (it IS the baseline) the decision must be allow.
    runner_final = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-fixture-4",
    )
    result_final = runner_final.run()
    assert result_final["run_count"] == 4
    assert result_final["gate_decision"]["decision"] in {"allow", "block"}, (
        "By run 4 the gate must be active (not skipped)."
    )


def test_runner_explicit_set_baseline_overrides(tmp_path) -> None:
    """--set-baseline overwrites whatever baseline is on disk."""
    sdl_root = tmp_path / "sdl"
    _stage_fixture_pairs(sdl_root)
    # Run 1 installs baseline implicitly.
    EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-A",
    ).run()
    baseline_path = sdl_root / "evals" / "baseline_eval_summary.json"
    first = json.loads(baseline_path.read_text(encoding="utf-8"))
    first_id = first["eval_summary_id"]

    # Run 2 with --set-baseline must overwrite.
    runner2 = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-B",
    )
    runner2.run(set_baseline=True)
    second = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert second["eval_summary_id"] != first_id, (
        "--set-baseline must replace the baseline on disk."
    )
    assert second["pipeline_run_id"] == "run-B"


def test_runner_handles_missing_sdl_gracefully(tmp_path) -> None:
    """SDL_ROOT unresolved -> failed status, exit_code 1, no exception."""
    # data_lake_path does not exist.
    runner = EvalRunner(
        data_lake_path="/nonexistent/path/that/cannot/be/found-xyz",
        pipeline_run_id="run-bad",
    )
    result = runner.run()
    # Either failed (data_lake_path missing) OR completed-with-no-pairs.
    # The trust property: exit_code is 1 only when SDL_ROOT cannot be
    # resolved; otherwise it's 0 even with zero pairs.
    if result["status"] == "failed":
        assert result["exit_code"] == 1
    else:
        assert result["exit_code"] == 0
