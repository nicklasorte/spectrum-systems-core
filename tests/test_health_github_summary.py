"""GitHub Actions summary tests for the eight failure classes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.health.eval_integrity import (
    append_github_summary,
    evaluate_upstream,
)


@pytest.fixture
def gh_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "summary.md"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


def _write_orch(
    lake: Path,
    run_id: str,
    *,
    stage_status: str,
    attempted: int,
    succeeded: int,
    blocked: int,
) -> None:
    d = lake / "store" / "artifacts" / "orchestration"
    d.mkdir(parents=True, exist_ok=True)
    artifact = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": run_id,
        "source_id": "src-1",
        "chunks_attempted": attempted,
        "chunks_succeeded": succeeded,
        "chunks_blocked": blocked,
        "block_reasons": {
            "rate_limit_exhausted": 0,
            "empty_response": 0,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": stage_status,
    }
    (d / f"{run_id}.json").write_text(json.dumps(artifact), encoding="utf-8")


def test_blocked_message_shown_when_synthesize_failed(
    tmp_path: Path, gh_summary: Path
) -> None:
    """Red Team 2: summary shows BLOCKED message instead of 0.000."""
    _write_orch(tmp_path, "r1", stage_status="failed", attempted=5, succeeded=0, blocked=5)
    findings, should_run = evaluate_upstream("r1", tmp_path, scores_are_zero=True)
    assert should_run is False
    append_github_summary(
        findings,
        blocked_message="Eval blocked: synthesize failed upstream. Fix synthesize before scoring.",
    )
    body = gh_summary.read_text(encoding="utf-8")
    assert "Eval blocked" in body
    assert "synthesize failed" in body
    assert "0.000" not in body
    assert "upstream_failure_eval_blocked" in body


def test_clean_run_summary_no_findings(tmp_path: Path, gh_summary: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="ok", attempted=10, succeeded=10, blocked=0)
    findings, _ = evaluate_upstream("r1", tmp_path)
    assert findings == []
    append_github_summary(findings)
    body = gh_summary.read_text(encoding="utf-8")
    assert "All health checks passed" in body
