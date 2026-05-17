"""Phase O.3: per-transcript pipeline run summary tests."""
from __future__ import annotations

import json
from pathlib import Path

from spectrum_systems_core.health import run_summary as rs


def _write_orchestration(
    data_lake: Path,
    *,
    source_id: str,
    chunks_attempted: int,
    chunks_succeeded: int,
    chunks_blocked: int,
    stage_status: str,
    block_reasons: dict[str, int] | None = None,
    run_id: str | None = None,
) -> None:
    orch_dir = data_lake / "store" / "artifacts" / "orchestration"
    orch_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or f"run-{source_id}"
    artifact = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": rid,
        "source_id": source_id,
        "chunks_attempted": chunks_attempted,
        "chunks_succeeded": chunks_succeeded,
        "chunks_blocked": chunks_blocked,
        "block_reasons": block_reasons
        or {
            "rate_limit_exhausted": 0,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": stage_status,
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    (orch_dir / f"{rid}_extraction.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    )


_FINDING_COUNTER = {"n": 0}


def _write_finding(data_lake: Path, *, finding_code: str, severity: str) -> None:
    health_dir = data_lake / "store" / "artifacts" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    _FINDING_COUNTER["n"] += 1
    fid = f"finding-{finding_code}-{severity}-{_FINDING_COUNTER['n']}"
    artifact = {
        "artifact_type": "health_finding",
        "schema_version": "1.0.0",
        "finding_id": fid,
        "finding_code": finding_code,
        "severity": severity,
        "pipeline_run_id": "run-1",
        "detected_at": "2025-01-01T00:00:00+00:00",
        "context": {},
        "remediation": "",
    }
    (health_dir / (fid + ".json")).write_text(json.dumps(artifact))


def test_summary_renders_for_three_transcripts(tmp_path):
    _write_orchestration(
        tmp_path,
        source_id="ok-transcript",
        chunks_attempted=100,
        chunks_succeeded=100,
        chunks_blocked=0,
        stage_status="ok",
    )
    _write_orchestration(
        tmp_path,
        source_id="partial-transcript",
        chunks_attempted=80,
        chunks_succeeded=75,
        chunks_blocked=5,
        stage_status="partial",
        block_reasons={
            "rate_limit_exhausted": 2,
            "empty_response": 3,
            "parse_error": 0,
            "other": 0,
        },
    )
    _write_orchestration(
        tmp_path,
        source_id="failed-transcript",
        chunks_attempted=50,
        chunks_succeeded=0,
        chunks_blocked=50,
        stage_status="failed",
        block_reasons={
            "rate_limit_exhausted": 50,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
    )

    summary = rs.build_summary(tmp_path, "run-pipeline-23")
    assert summary["artifact_type"] == "pipeline_run_summary"
    assert summary["schema_version"] == "1.0.0"
    assert summary["pipeline_run_id"] == "run-pipeline-23"
    sids = [t["source_id"] for t in summary["transcripts"]]
    assert set(sids) == {
        "ok-transcript",
        "partial-transcript",
        "failed-transcript",
    }
    md = rs.render_markdown(summary)
    assert "Pipeline Run Summary" in md
    assert "ok-transcript" in md
    assert "partial-transcript" in md
    assert "failed-transcript" in md
    # Stage glyphs.
    assert "clm✓" in md
    assert "clm⚠" in md
    assert "clm✗" in md


def test_missing_orchestration_rendered_as_dash(tmp_path):
    summary = rs.build_summary(
        tmp_path,
        "run-1",
        source_ids=["missing-transcript"],
    )
    assert summary["transcripts"][0]["has_orchestration_artifact"] is False
    assert summary["transcripts"][0]["stage_status"] == "missing"
    md = rs.render_markdown(summary)
    assert "no orchestration artifact" in md


def test_blocked_breakdown_aggregates(tmp_path):
    _write_orchestration(
        tmp_path,
        source_id="t1",
        chunks_attempted=10,
        chunks_succeeded=8,
        chunks_blocked=2,
        stage_status="partial",
        block_reasons={
            "rate_limit_exhausted": 2,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
    )
    _write_orchestration(
        tmp_path,
        source_id="t2",
        chunks_attempted=20,
        chunks_succeeded=18,
        chunks_blocked=2,
        stage_status="partial",
        block_reasons={
            "rate_limit_exhausted": 1,
            "empty_response": 1,
            "parse_error": 0,
            "other": 0,
        },
    )
    summary = rs.build_summary(tmp_path, "run-1")
    breakdown = summary["block_reason_breakdown"]
    assert breakdown["rate_limit_exhausted"] == 3
    assert breakdown["empty_response"] == 1


def test_health_findings_appear_in_summary(tmp_path):
    _write_finding(tmp_path, finding_code="model_registry_drift", severity="warn")
    _write_finding(tmp_path, finding_code="model_registry_drift", severity="warn")
    _write_finding(
        tmp_path,
        finding_code="confidence_field_missing",
        severity="warn",
    )
    summary = rs.build_summary(tmp_path, "run-1")
    codes = [
        (f["finding_code"], f["severity"], f["count"])
        for f in summary["health_findings"]
    ]
    assert ("model_registry_drift", "warn", 2) in codes
    assert ("confidence_field_missing", "warn", 1) in codes
    md = rs.render_markdown(summary)
    assert "model_registry_drift" in md
    assert "warn" in md


def test_artifact_written_and_passes_schema(tmp_path):
    _write_orchestration(
        tmp_path,
        source_id="t1",
        chunks_attempted=1,
        chunks_succeeded=1,
        chunks_blocked=0,
        stage_status="ok",
    )
    summary = rs.build_summary(tmp_path, "run-write")
    out = rs.write_artifact(summary, data_lake_path=tmp_path)
    assert out is not None and out.is_file()
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["artifact_type"] == "pipeline_run_summary"
    from spectrum_systems_core.validation import validate_artifact

    validate_artifact(on_disk, "pipeline_run_summary")


def test_emit_step_summary_falls_back_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rs.emit_step_summary("## hello local\n")
    out = capsys.readouterr().out
    assert "hello local" in out


def test_emit_step_summary_appends_to_github_path(tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    target.write_text("prefix\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    rs.emit_step_summary("## appended\n")
    text = target.read_text(encoding="utf-8")
    assert "prefix" in text
    assert "## appended" in text


def test_main_cli_writes_artifact_and_summary(tmp_path, monkeypatch, capsys):
    _write_orchestration(
        tmp_path,
        source_id="t1",
        chunks_attempted=1,
        chunks_succeeded=1,
        chunks_blocked=0,
        stage_status="ok",
    )
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = rs.main(
        [
            "--data-lake",
            str(tmp_path),
            "--pipeline-run-id",
            "run-cli",
            "--output-format",
            "github_actions_summary",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Pipeline Run Summary" in out
    artifact_path = (
        tmp_path / "store" / "artifacts" / "pipeline_runs" / "run-cli.json"
    )
    assert artifact_path.is_file()


def test_long_source_id_truncated_in_markdown(tmp_path):
    sid = "z" * 100
    _write_orchestration(
        tmp_path,
        source_id=sid,
        chunks_attempted=1,
        chunks_succeeded=1,
        chunks_blocked=0,
        stage_status="ok",
    )
    summary = rs.build_summary(tmp_path, "run-1")
    md = rs.render_markdown(summary)
    # Artifact retains the full id...
    assert summary["transcripts"][0]["source_id"] == sid
    # ...but the markdown table truncates with an ellipsis.
    assert "..." in md
    assert sid not in md
