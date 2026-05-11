"""Phase O.6 — tests for compile-findings CLI command."""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.cli import compile_findings_cli
from spectrum_systems_core.verification import (
    compile_findings,
    write_verification_findings,
)

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


def _write_pipeline_state_record(
    sdl: Path,
    *,
    validation_failures: dict | None = None,
    expected: dict | None = None,
    kind_only: int = 0,
) -> Path:
    target = sdl / "verifications"
    target.mkdir(parents=True, exist_ok=True)
    record_id = str(uuid.uuid4())
    expected_defaults = {
        "source_record_count": 0,
        "minutes_record_count": 0,
        "confirmed_pair_count": 0,
        "chunks_files_present": 0,
        "meeting_extraction_count": 0,
        "alignment_result_count": 0,
        "eval_result_count": 0,
        "baseline_eval_summary_present": False,
        "glossary_term_count": 0,
    }
    if expected:
        expected_defaults.update(expected)
    record = {
        "pipeline_state_record_id": record_id,
        "artifact_type": "pipeline_state_record",
        "schema_version": "1.0.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "data_lake_path": "",
        "sdl_root": str(sdl),
        "total_artifacts_scanned": 0,
        "artifacts_by_type": {},
        "artifacts_by_schema_version": {},
        "validation_failures_by_type": validation_failures or {},
        "artifacts_with_artifact_kind_only": kind_only,
        "artifacts_with_both_fields": 0,
        "artifacts_with_artifact_type_only": 0,
        "expected_artifacts": expected_defaults,
        "next_required_actions": [],
        "warnings": [],
        "provenance": {"produced_by": "verify-pipeline-state"},
    }
    fp = target / f"{record_id}.json"
    fp.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fp


def _write_eval_summary(sdl: Path, **fields) -> Path:
    target = sdl / "evals"
    target.mkdir(parents=True, exist_ok=True)
    body = {
        "eval_summary_id": str(uuid.uuid4()),
        "pipeline_run_id": "run-x",
        "artifact_type": "eval_summary",
        "schema_version": "1.1.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "pairs_evaluated": 1,
        "pairs_skipped_pending_review": 0,
        "aggregate_coverage": 0.75,
        "aggregate_precision": 0.7,
        "total_items_requiring_review": 0,
        "by_chunking_strategy": {
            "speaker_turn": {"coverage": 0.75, "precision": 0.7, "pairs_count": 1},
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
    body.update(fields)
    fp = target / f"eval_summary_{body['pipeline_run_id']}.json"
    fp.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fp


def test_compiles_findings_from_pipeline_state(tmp_path: Path) -> None:
    """A pipeline_state_record artifact ON DISK with validation_failures must
    produce corresponding findings in the verification_findings artifact."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(
        sdl, validation_failures={"source_record": 2}
    )

    record = compile_findings(cycle_id="phase-O-test", sdl_root=sdl)
    titles = [f["title"] for f in record["findings"]]
    assert any(
        "schema_validation_failures_for_source_record" in t for t in titles
    )


def test_includes_metrics_snapshot_from_eval_summary(tmp_path: Path) -> None:
    """aggregate_coverage from the eval_summary must propagate into the
    metrics_snapshot of the compiled verification_findings artifact."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(sdl)
    _write_eval_summary(sdl, aggregate_coverage=0.75)

    record = compile_findings(cycle_id="phase-O-test", sdl_root=sdl)
    assert record["metrics_snapshot"]["aggregate_coverage"] == 0.75


def test_findings_artifact_validates_against_schema(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(sdl)
    _write_eval_summary(sdl)

    record = compile_findings(cycle_id="phase-O-test", sdl_root=sdl)
    target = write_verification_findings(record, sdl_root=sdl)
    assert target is not None
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    schema = json.loads(
        (CONTRACT_DIR / "verification" / "verification_findings.schema.json")
        .read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(on_disk)


def test_compile_with_no_eval_summary_writes_valid_artifact(
    tmp_path: Path,
) -> None:
    """Sev-1 Red-Team scenario #5."""
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(sdl)
    # No eval_summary on disk.

    record = compile_findings(cycle_id="phase-O-test", sdl_root=sdl)
    snap = record["metrics_snapshot"]
    assert snap["aggregate_coverage"] is None
    assert snap["aggregate_precision"] is None
    assert snap["items_requiring_review_total"] is None
    assert snap["partial_run_warning"] is None
    # Artifact still validates.
    schema = json.loads(
        (CONTRACT_DIR / "verification" / "verification_findings.schema.json")
        .read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(record)


def test_cli_compile_findings_writes_artifact_and_summary(
    tmp_path: Path, monkeypatch
) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(sdl)
    _write_eval_summary(sdl)
    monkeypatch.setenv("SDL_ROOT", str(sdl))
    step_summary = tmp_path / "GH"
    step_summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))

    buf = io.StringIO()
    rc = compile_findings_cli(
        data_lake=str(tmp_path), cycle_id="phase-O-cli", out_stream=buf
    )
    assert rc == 0
    # The artifact is on disk under verifications/.
    artifacts = list((sdl / "verifications").glob("*.json"))
    assert artifacts, "expected verification_findings artifact on disk"
    # GitHub step summary was appended to.
    assert "compile-findings" in step_summary.read_text(encoding="utf-8")


def test_partial_run_warning_propagates_as_finding(tmp_path: Path) -> None:
    sdl = tmp_path / "sdl"
    sdl.mkdir()
    _write_pipeline_state_record(sdl)
    _write_eval_summary(
        sdl,
        partial_run_warning=True,
        partial_run_detail={
            "expected": 13,
            "actual": 10,
            "missing_source_ids": ["x", "y", "z"],
        },
    )
    record = compile_findings(cycle_id="phase-O-test", sdl_root=sdl)
    sev_1 = [f for f in record["findings"] if f["severity"] == "sev_1"]
    assert any(
        f["title"] == "eval_summary_partial_run_warning" for f in sev_1
    )
