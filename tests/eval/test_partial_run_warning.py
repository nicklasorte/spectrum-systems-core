"""Phase O.4 — tests for partial_run_warning + --set-baseline refusal."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

from spectrum_systems_core.evals.m4 import EvalRunner


CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


def _confirmed_pair(source_id: str) -> Dict[str, Any]:
    """A confirmed pair that does NOT carry fixture_extracted_items, so the
    runner must look on disk for a meeting_extraction artifact."""
    return {
        "pair_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source_artifact_id": str(uuid.uuid4()),
        "minutes_artifact_id": str(uuid.uuid4()),
        "fixture_source_id": source_id,
        "fixture_minutes_text": "(no inline items)",
    }


def _write_pair(sdl_root: Path, pair: Dict[str, Any]) -> Path:
    target = sdl_root / "ground_truth"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{pair['pair_id']}.json"
    fp.write_text(
        json.dumps(pair, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fp


def _seed_meeting_extraction(sdl_root: Path, source_id: str) -> Path:
    target = sdl_root / "extractions"
    target.mkdir(parents=True, exist_ok=True)
    fp = target / f"{source_id}_meeting_extraction.json"
    fp.write_text(
        json.dumps(
            {
                "artifact_type": "meeting_extraction",
                "schema_version": "1.0.0",
                "source_id": source_id,
                "meeting_extraction_id": str(uuid.uuid4()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fp


def test_partial_run_warning_when_extractions_missing(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    # 13 confirmed pairs total. 10 have meeting_extraction on disk; 3 do not.
    source_ids = [f"src_{i:02d}" for i in range(13)]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    for sid in source_ids[:10]:
        _seed_meeting_extraction(sdl, sid)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-partial",
    )
    result = runner.run()
    assert result["status"] == "completed"
    summary = result["summary"]
    assert summary["partial_run_warning"] is True
    detail = summary["partial_run_detail"]
    assert detail is not None
    assert detail["expected"] == 13
    assert detail["actual"] == 10
    assert len(detail["missing_source_ids"]) == 3
    # The 3 missing are the LAST three source_ids by construction.
    assert set(detail["missing_source_ids"]) == set(source_ids[10:])

    # Implicit-baseline rule must NOT install a baseline on a partial run.
    assert summary["is_baseline"] is False
    assert not (sdl / "evals" / "baseline_eval_summary.json").exists()


def test_set_baseline_refuses_on_partial_run(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    source_ids = ["a", "b", "c"]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    _seed_meeting_extraction(sdl, "a")  # only one extraction; partial run

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-refusal",
    )
    result = runner.run(set_baseline=True)
    assert result["exit_code"] == 1
    assert result.get("partial_run_warning") is True
    assert "partial_run_warning_blocks_set_baseline" in str(
        result.get("reason", "")
    )
    # No baseline file was written.
    assert not (sdl / "evals" / "baseline_eval_summary.json").exists()
    # No eval_summary should be written either — refusal blocks before
    # the per-pair eval loop.
    assert not list((sdl / "evals").glob("eval_summary_*.json")) if (
        sdl / "evals"
    ).exists() else True


def test_set_baseline_succeeds_on_complete_run(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    source_ids = ["a", "b"]
    for sid in source_ids:
        _write_pair(sdl, _confirmed_pair(sid))
    for sid in source_ids:
        _seed_meeting_extraction(sdl, sid)

    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-complete",
    )
    result = runner.run(set_baseline=True)
    assert result["exit_code"] == 0
    # Baseline file was installed.
    assert (sdl / "evals" / "baseline_eval_summary.json").is_file()


def test_eval_summary_schema_includes_partial_run_fields() -> None:
    """Schema 1.1.0 must declare the new fields as required."""
    schema = json.loads(
        (CONTRACT_DIR / "eval" / "eval_summary.schema.json")
        .read_text(encoding="utf-8")
    )
    required = schema.get("required", [])
    assert "partial_run_warning" in required
    assert "partial_run_detail" in required
    assert schema["properties"]["schema_version"]["const"] == "1.1.0"


def test_zero_confirmed_pairs_does_not_divide_by_zero(tmp_path: Path) -> None:
    """Sev-1 Red-Team scenario #3."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    runner = EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl),
        pipeline_run_id="run-empty",
    )
    result = runner.run()
    assert result["status"] == "completed"
    summary = result["summary"]
    assert summary["partial_run_warning"] is False
    assert summary["partial_run_detail"] == {
        "expected": 0,
        "actual": 0,
        "missing_source_ids": [],
    }
