"""Phase O.4: eval pair provenance + per_source_metrics tests."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

from spectrum_systems_core.evals.m4.runner import EvalRunner


def _write_pair(
    sdl_root: Path,
    *,
    pair_id: str,
    source_id: str | None,
    minutes_text: str = "DECISION: Approve the launch.",
    extracted_items: List[Dict[str, Any]] | None = None,
    status: str = "confirmed",
) -> None:
    pairs_dir = sdl_root / "ground_truth"
    pairs_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "pair_id": pair_id,
        "source_artifact_id": str(uuid.uuid4()),
        "minutes_artifact_id": str(uuid.uuid4()),
        "status": status,
        "fixture_minutes_text": minutes_text,
        "fixture_extracted_items": extracted_items
        or [{"text": "Approved the launch", "kind": "decision"}],
        "fixture_chunking_strategy": "speaker_turn",
    }
    if source_id is not None:
        rec["fixture_source_id"] = source_id
    (pairs_dir / f"{pair_id}.json").write_text(json.dumps(rec))


def _run(tmp_path: Path) -> EvalRunner:
    sdl_root = tmp_path / "store" / "artifacts"
    sdl_root.mkdir(parents=True, exist_ok=True)
    return EvalRunner(
        data_lake_path=str(tmp_path),
        sdl_root=str(sdl_root),
        pipeline_run_id="run-test",
    )


def test_pair_breakdown_contains_source_and_agenda(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root,
        pair_id=str(uuid.uuid4()),
        source_id="source-a",
    )
    _write_pair(
        sdl_root,
        pair_id=str(uuid.uuid4()),
        source_id="source-b",
    )
    runner = _run(tmp_path)
    result = runner.run()
    summary = result["summary"]
    breakdown = summary["pair_breakdown"]
    assert len(breakdown) == 2
    for entry in breakdown:
        assert entry["source_id"] in {"source-a", "source-b"}
        assert entry["agenda_item_id"] is None
        assert entry["status"] in {"matched", "unmatched"}
        assert 0.0 <= entry["match_score"] <= 1.0


def test_per_source_metrics_aggregates(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    for sid in ("alpha", "beta", "gamma"):
        for j in range(2):
            _write_pair(
                sdl_root,
                pair_id=str(uuid.uuid4()),
                source_id=sid,
            )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    per_source = summary["per_source_metrics"]
    assert per_source is not None
    assert set(per_source.keys()) == {"alpha", "beta", "gamma"}
    for sid, bucket in per_source.items():
        assert bucket["pairs"] == 2
        assert 0.0 <= bucket["coverage"] <= 1.0
        assert 0.0 <= bucket["precision"] <= 1.0


def test_per_source_metrics_suppressed_for_single_source(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="only-source",
    )
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="only-source",
    )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    assert summary["per_source_metrics"] is None


def test_missing_source_id_emits_info_finding(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root,
        pair_id=str(uuid.uuid4()),
        source_id=None,  # No source_id field -> info finding.
    )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    breakdown = summary["pair_breakdown"]
    assert len(breakdown) == 1
    assert breakdown[0]["source_id"] is None
    # Info finding written under store/artifacts/health/.
    health_dir = tmp_path / "store" / "artifacts" / "health"
    finding_files = list(health_dir.glob("*.json")) if health_dir.is_dir() else []
    assert finding_files, "expected at least one health finding artifact"
    finding_codes = {
        json.loads(p.read_text(encoding="utf-8")).get("finding_code")
        for p in finding_files
    }
    assert "eval_pair_missing_source_id" in finding_codes


def test_agenda_item_id_null_when_phase_w_inactive(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="source-a",
    )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    for entry in summary["pair_breakdown"]:
        assert entry["agenda_item_id"] is None


def test_summary_schema_version_2_0_0_validates(tmp_path):
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="source-a",
    )
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="source-b",
    )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    assert summary["schema_version"] == "2.0.0"
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "schemas"
        / "eval"
        / "eval_summary.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(summary)


def test_legacy_1_1_0_summary_still_readable():
    """An existing v1.1.0 artifact must still validate against the schema."""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "schemas"
        / "eval"
        / "eval_summary.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    legacy = {
        "eval_summary_id": str(uuid.uuid4()),
        "pipeline_run_id": "run-legacy",
        "artifact_type": "eval_summary",
        "schema_version": "1.1.0",
        "created_at": "2024-01-01T00:00:00+00:00",
        "pairs_evaluated": 0,
        "pairs_skipped_pending_review": 0,
        "aggregate_coverage": 0.0,
        "aggregate_precision": 0.0,
        "total_items_requiring_review": 0,
        "by_chunking_strategy": {
            "speaker_turn": {"coverage": 0.0, "precision": 0.0, "pairs_count": 0},
            "character_count_fallback": {"coverage": 0.0, "precision": 0.0, "pairs_count": 0},
        },
        "eval_results": [],
        "is_baseline": False,
        "baseline_eval_summary_id": None,
        "regression_detected": False,
        "regression_detail": [],
        "partial_run_warning": False,
        "partial_run_detail": None,
        "provenance": {"produced_by": "EvalRunner"},
    }
    jsonschema.Draft202012Validator(schema).validate(legacy)


def test_pair_counted_once_per_pair_id(tmp_path):
    """A pair must not double-count even if multiple agenda_items hint at it."""
    sdl_root = tmp_path / "store" / "artifacts"
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="source-a",
    )
    _write_pair(
        sdl_root, pair_id=str(uuid.uuid4()), source_id="source-b",
    )
    runner = _run(tmp_path)
    summary = runner.run()["summary"]
    per_source = summary["per_source_metrics"]
    assert per_source["source-a"]["pairs"] == 1
    assert per_source["source-b"]["pairs"] == 1
