"""Classes 1, 2, 4, 7: eval integrity tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.health.eval_integrity import (
    audit_pair_coverage,
    check_upstream_health,
    detect_registry_drift,
    evaluate_upstream,
    get_registry_hash,
    pair_audit_finding,
    upstream_health_annotation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orch_dir(lake: Path) -> Path:
    return lake / "store" / "artifacts" / "orchestration"


def _write_orch(
    lake: Path,
    run_id: str,
    *,
    stage_status: str,
    attempted: int,
    succeeded: int,
    blocked: int,
    block_reasons: dict[str, int] | None = None,
) -> Path:
    d = _orch_dir(lake)
    d.mkdir(parents=True, exist_ok=True)
    reasons = {
        "rate_limit_exhausted": 0,
        "empty_response": 0,
        "parse_error": 0,
        "other": 0,
    }
    if block_reasons:
        reasons.update(block_reasons)
    artifact = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": run_id,
        "source_id": "src-1",
        "chunks_attempted": attempted,
        "chunks_succeeded": succeeded,
        "chunks_blocked": blocked,
        "block_reasons": reasons,
        "stage_status": stage_status,
    }
    path = d / f"{run_id}.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _config_dir(lake: Path) -> Path:
    return lake / "store" / "artifacts" / "config"


# ---------------------------------------------------------------------------
# Classes 1 + 2 — upstream gating
# ---------------------------------------------------------------------------


def test_synthesize_failed_emits_halt_and_blocks_eval(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="failed", attempted=5, succeeded=0, blocked=5)
    findings, should_run = evaluate_upstream("r1", tmp_path)
    assert should_run is False
    codes = {f.finding_code for f in findings}
    assert "upstream_failure_eval_blocked" in codes
    halt = next(f for f in findings if f.finding_code == "upstream_failure_eval_blocked")
    assert halt.severity == "halt"


def test_chunks_blocked_warn_but_eval_runs(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="partial", attempted=10, succeeded=5, blocked=5)
    findings, should_run = evaluate_upstream("r1", tmp_path)
    assert should_run is True
    codes = {f.finding_code for f in findings}
    assert "upstream_failure_eval_invalid" in codes
    warn = next(f for f in findings if f.finding_code == "upstream_failure_eval_invalid")
    assert warn.severity == "warn"
    assert warn.context["chunks_blocked"] == 5


def test_clean_upstream_no_findings(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="ok", attempted=10, succeeded=10, blocked=0)
    findings, should_run = evaluate_upstream("r1", tmp_path)
    assert should_run is True
    assert findings == []


def test_eval_zero_cause_upstream_annotation(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="failed", attempted=5, succeeded=0, blocked=5)
    findings, _ = evaluate_upstream("r1", tmp_path, scores_are_zero=True)
    codes = {f.finding_code for f in findings}
    assert "eval_zero_cause_upstream" in codes
    cause = next(f for f in findings if f.finding_code == "eval_zero_cause_upstream")
    assert cause.severity == "warn"


def test_eval_zero_cause_extraction_when_upstream_clean(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="ok", attempted=10, succeeded=10, blocked=0)
    findings, _ = evaluate_upstream("r1", tmp_path, scores_are_zero=True)
    codes = {f.finding_code for f in findings}
    assert "eval_zero_cause_extraction" in codes


def test_missing_orchestration_info_finding(tmp_path: Path) -> None:
    """Red Team 1: missing orchestration record -> info, eval proceeds."""
    findings, should_run = evaluate_upstream("never-existed", tmp_path)
    assert should_run is True
    codes = {f.finding_code for f in findings}
    assert "no_prior_orchestration_artifact" in codes
    info = next(f for f in findings if f.finding_code == "no_prior_orchestration_artifact")
    assert info.severity == "info"


def test_upstream_health_annotation_block(tmp_path: Path) -> None:
    _write_orch(tmp_path, "r1", stage_status="partial", attempted=10, succeeded=7, blocked=3)
    health = check_upstream_health("r1", tmp_path)
    annotation = upstream_health_annotation(health)
    assert annotation["chunks_blocked"] == 3
    assert annotation["scores_may_be_understated"] is True
    assert annotation["synthesize_succeeded"] is True


# ---------------------------------------------------------------------------
# Class 4 — pair coverage audit
# ---------------------------------------------------------------------------


def test_all_confirmed_evaluated_no_finding() -> None:
    pairs = [{"pair_id": "a", "status": "confirmed"}, {"pair_id": "b", "status": "confirmed"}]
    results = [{"pair_id": "a"}, {"pair_id": "b"}]
    audit = audit_pair_coverage(results, pairs)
    assert pair_audit_finding(audit) is None


def test_pending_pairs_emit_warn() -> None:
    pairs = [
        {"pair_id": "a", "status": "confirmed"},
        {"pair_id": "b", "status": "pending_review"},
    ]
    results = [{"pair_id": "a"}]
    audit = audit_pair_coverage(results, pairs)
    finding = pair_audit_finding(audit)
    assert finding is not None
    assert finding.finding_code == "eval_pairs_excluded"
    assert finding.severity == "warn"
    assert audit.pending_review == 1


def test_missing_pair_finding_includes_pair_ids() -> None:
    pairs = [
        {"pair_id": "a", "status": "confirmed"},
        {"pair_id": "b", "status": "confirmed"},
    ]
    results = [{"pair_id": "a"}]
    audit = audit_pair_coverage(results, pairs)
    finding = pair_audit_finding(audit)
    assert finding is not None
    assert "b" in finding.context["missing_pair_ids"]
    # Context must include pair_ids, not just counts.
    assert isinstance(finding.context["missing_pair_ids"], list)


# ---------------------------------------------------------------------------
# Class 7 — model registry drift
# ---------------------------------------------------------------------------


def _write_registry(lake: Path, body: dict) -> Path:
    d = _config_dir(lake)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "model_registry.json"
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    return path


def test_same_hash_no_finding(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"extractor": "model-a"})
    h = get_registry_hash(tmp_path)
    current, finding = detect_registry_drift(tmp_path, h, baseline_models={"extractor": "model-a"})
    assert current == h
    assert finding is None


def test_different_hash_emits_warn(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"extractor": "model-b"})
    _, finding = detect_registry_drift(
        tmp_path,
        baseline_hash="0000000000000000",
        baseline_models={"extractor": "model-a"},
    )
    assert finding is not None
    assert finding.finding_code == "model_registry_drift"
    assert finding.severity == "warn"
    assert "extractor" in finding.context["changed_models"]
    assert finding.context["baseline_hash"] == "0000000000000000"


def test_drift_does_not_block_eval(tmp_path: Path) -> None:
    """Red Team 2: drift is warn, never halt."""
    _write_registry(tmp_path, {"extractor": "model-b"})
    _, finding = detect_registry_drift(
        tmp_path,
        baseline_hash="ffff",
        baseline_models={"extractor": "model-a"},
    )
    assert finding is not None
    assert finding.severity != "halt"


def test_drift_finding_includes_both_hashes(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"extractor": "model-z"})
    h = get_registry_hash(tmp_path)
    _, finding = detect_registry_drift(
        tmp_path,
        baseline_hash="abc123abc123abc1",
        baseline_models={"extractor": "model-a"},
    )
    assert finding is not None
    assert finding.context["current_hash"] == h
    assert finding.context["baseline_hash"] == "abc123abc123abc1"
