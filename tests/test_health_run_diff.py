"""Phase O.5: pipeline run diff tool tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.health import run_diff
from spectrum_systems_core.health.run_diff import (
    PipelineRunSummaryMissing,
    diff_runs,
    render_markdown,
    write_diff_artifact,
)


def _make_transcript(
    *,
    source_id: str,
    chunks_attempted: int,
    chunks_blocked: int,
    stage_status: str,
) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "chunks_attempted": chunks_attempted,
        "chunks_succeeded": chunks_attempted - chunks_blocked,
        "chunks_blocked": chunks_blocked,
        "stage_status": stage_status,
        "has_orchestration_artifact": True,
        "block_reasons": {},
        "synthesize_ok": None,
    }


def _write_summary(
    tmp_path: Path,
    *,
    run_id: str,
    transcripts: List[Dict[str, Any]],
    block_reasons: Dict[str, int] | None = None,
    findings: List[str] | None = None,
    eval_metrics: Dict[str, float] | None = None,
) -> None:
    runs_dir = tmp_path / "store" / "artifacts" / "pipeline_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    total_attempted = sum(t["chunks_attempted"] for t in transcripts)
    total_blocked = sum(t["chunks_blocked"] for t in transcripts)
    total_succeeded = sum(t["chunks_succeeded"] for t in transcripts)
    summary = {
        "artifact_type": "pipeline_run_summary",
        "schema_version": "1.0.0",
        "pipeline_run_id": run_id,
        "created_at": "2025-01-01T00:00:00+00:00",
        "transcripts": transcripts,
        "totals": {
            "transcripts": len(transcripts),
            "chunks_attempted": total_attempted,
            "chunks_succeeded": total_succeeded,
            "chunks_blocked": total_blocked,
            "blocked_rate": (
                total_blocked / total_attempted if total_attempted else 0.0
            ),
        },
        "block_reason_breakdown": block_reasons or {},
        "health_findings": [
            {"finding_code": code, "severity": "warn", "count": 1}
            for code in (findings or [])
        ],
    }
    if eval_metrics is not None:
        summary["eval_metrics"] = eval_metrics
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )


def test_diff_computes_chunks_blocked_delta(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-23",
        transcripts=[
            _make_transcript(
                source_id="t1", chunks_attempted=100, chunks_blocked=12, stage_status="partial",
            ),
            _make_transcript(
                source_id="t2", chunks_attempted=100, chunks_blocked=32, stage_status="partial",
            ),
        ],
        block_reasons={"empty_response": 44},
    )
    _write_summary(
        tmp_path,
        run_id="run-24",
        transcripts=[
            _make_transcript(
                source_id="t1", chunks_attempted=100, chunks_blocked=0, stage_status="ok",
            ),
            _make_transcript(
                source_id="t2", chunks_attempted=100, chunks_blocked=0, stage_status="ok",
            ),
        ],
        block_reasons={"empty_response": 0},
    )
    diff = diff_runs("run-23", "run-24", tmp_path)
    by_sid = {row["source_id"]: row for row in diff["per_transcript_diff"]}
    assert by_sid["t1"]["chunks_blocked_delta"] == -12
    assert by_sid["t2"]["chunks_blocked_delta"] == -32
    assert diff["totals_diff"]["chunks_blocked_delta"] == -44


def test_stage_improved_true_when_partial_to_ok(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(
                source_id="t", chunks_attempted=10, chunks_blocked=5, stage_status="partial",
            )
        ],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(
                source_id="t", chunks_attempted=10, chunks_blocked=0, stage_status="ok",
            )
        ],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    row = diff["per_transcript_diff"][0]
    assert row["stage_improved"] is True


def test_stage_improved_false_on_regression(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(
                source_id="t", chunks_attempted=10, chunks_blocked=0, stage_status="ok",
            )
        ],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(
                source_id="t", chunks_attempted=10, chunks_blocked=5, stage_status="partial",
            )
        ],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    row = diff["per_transcript_diff"][0]
    assert row["stage_improved"] is False


def test_new_and_resolved_findings(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[],
        findings=["upstream_failure_eval_blocked"],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[],
        findings=["model_registry_drift"],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    assert diff["new_findings_in_b"] == ["model_registry_drift"]
    assert diff["resolved_findings_in_b"] == ["upstream_failure_eval_blocked"]


def test_eval_diff_coverage_and_precision(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[],
        eval_metrics={"aggregate_coverage": 0.0, "aggregate_precision": 0.0},
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[],
        eval_metrics={"aggregate_coverage": 0.72, "aggregate_precision": 0.85},
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    eval_diff = diff["eval_diff"]
    assert eval_diff is not None
    assert eval_diff["coverage_a"] == 0.0
    assert pytest.approx(eval_diff["coverage_b"], rel=1e-6) == 0.72
    assert pytest.approx(eval_diff["coverage_delta"], rel=1e-6) == 0.72
    assert pytest.approx(eval_diff["precision_delta"], rel=1e-6) == 0.85


def test_missing_summary_halts_with_finding(tmp_path):
    with pytest.raises(PipelineRunSummaryMissing):
        diff_runs("missing-a", "missing-b", tmp_path)
    # CLI main path also produces an info finding.
    rc = run_diff.main(
        [
            "--data-lake", str(tmp_path),
            "--run-a-id", "missing-a",
            "--run-b-id", "missing-b",
            "--output-format", "json",
        ]
    )
    assert rc == 1
    health_dir = tmp_path / "store" / "artifacts" / "health"
    found = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (health_dir.glob("*.json") if health_dir.is_dir() else [])
    ]
    codes = {f.get("finding_code") for f in found}
    assert "pipeline_run_summary_missing" in codes


def test_diff_artifact_passes_schema(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(source_id="t", chunks_attempted=1, chunks_blocked=0, stage_status="ok"),
        ],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(source_id="t", chunks_attempted=1, chunks_blocked=0, stage_status="ok"),
        ],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    out = write_diff_artifact(diff, data_lake_path=tmp_path)
    assert out is not None and out.is_file()
    from spectrum_systems_core.validation import validate_artifact
    validate_artifact(diff, "pipeline_run_diff")


def test_markdown_renders_arrows(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(source_id="t", chunks_attempted=100, chunks_blocked=44, stage_status="partial"),
        ],
        eval_metrics={"aggregate_coverage": 0.0, "aggregate_precision": 0.0},
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(source_id="t", chunks_attempted=100, chunks_blocked=0, stage_status="ok"),
        ],
        eval_metrics={"aggregate_coverage": 0.72, "aggregate_precision": 0.85},
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    md = render_markdown(diff)
    assert "▼" in md or "▲" in md  # at least one directional arrow
    assert "Pipeline Run Diff" in md


def test_diff_handles_transcript_only_in_one_run(tmp_path):
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(source_id="t1", chunks_attempted=10, chunks_blocked=0, stage_status="ok"),
        ],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(source_id="t1", chunks_attempted=10, chunks_blocked=0, stage_status="ok"),
            _make_transcript(source_id="t2", chunks_attempted=5, chunks_blocked=0, stage_status="ok"),
        ],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    by_sid = {row["source_id"]: row for row in diff["per_transcript_diff"]}
    assert by_sid["t2"]["present_in_a"] is False
    assert by_sid["t2"]["present_in_b"] is True
    assert by_sid["t2"]["chunks_attempted_delta"] == 5


def test_long_source_id_truncated_in_markdown(tmp_path):
    sid = "z" * 100
    _write_summary(
        tmp_path,
        run_id="run-a",
        transcripts=[
            _make_transcript(source_id=sid, chunks_attempted=1, chunks_blocked=0, stage_status="ok"),
        ],
    )
    _write_summary(
        tmp_path,
        run_id="run-b",
        transcripts=[
            _make_transcript(source_id=sid, chunks_attempted=1, chunks_blocked=0, stage_status="ok"),
        ],
    )
    diff = diff_runs("run-a", "run-b", tmp_path)
    # Full id retained in the artifact.
    assert diff["per_transcript_diff"][0]["source_id"] == sid
    md = render_markdown(diff)
    assert "..." in md
    assert sid not in md
