"""Phase Y.3 — comparison control gate (F1 thresholds + env rollback)."""
from __future__ import annotations

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals.runner import run_required_evals


def _passing_eval(target):
    return new_artifact(
        artifact_type="eval_result",
        payload={
            "eval_type": "non_empty_payload",
            "target_artifact_id": target.artifact_id,
            "status": "pass",
            "score": 1.0,
            "reason_codes": [],
        },
        trace_id=target.trace_id,
        status="evaluated",
    )


def _comparison(total_f1, per_type):
    return new_artifact(
        artifact_type="extraction_alignment_comparison",
        payload={
            "artifact_type": "extraction_alignment_comparison",
            "schema_version": "1.0.0",
            "transcript_id": "m-y-gate",
            "ceiling_artifact_id": "ceil-1",
            "haiku_artifact_id": "haiku-1",
            "alignment_contract_version": "1.0.0",
            "per_type_metrics": per_type,
            "total_metrics": (
                {"recall": 0.8, "precision": 0.8, "f1": total_f1}
                if total_f1 is not None
                else None
            ),
            "aligned_pairs": [],
            "false_negatives": [],
        },
        trace_id="trace-gate",
        status="draft",
    )


def _pt(f1, ceiling_count):
    return {
        "recall": f1,
        "precision": f1,
        "f1": f1,
        "ceiling_count": ceiling_count,
        "haiku_count": ceiling_count,
        "true_positives": 0,
        "false_negatives": 0,
        "false_positives": 0,
    }


def test_repro_per_type_floor_blocks():
    """total f1 0.80 (above 0.70) but decision f1 0.0 with
    ceiling_count 5 (>= 3) -> block on the per-type floor, not the
    total threshold."""
    cmp = _comparison(0.80, {"decision": _pt(0.0, 5)})
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "block"
    assert (
        "comparison_per_type_f1_below_floor:decision"
        in decision.payload["reason_codes"]
    )


def test_repro_env_rollback_allows(monkeypatch):
    monkeypatch.setenv("COMPARISON_GATE_ENABLED", "false")
    cmp = _comparison(0.80, {"decision": _pt(0.0, 5)})
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "allow"


def test_total_f1_below_threshold_blocks():
    cmp = _comparison(0.55, {"decision": _pt(0.9, 5)})
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "block"
    assert (
        "comparison_total_f1_below_threshold"
        in decision.payload["reason_codes"]
    )


def test_failclosed_total_metrics_missing_blocks():
    """Red-team Pass 1 #2: a null total_metrics must BLOCK, never
    slip through as allow because the gated field is absent."""
    cmp = _comparison(None, {"decision": _pt(0.9, 5)})
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "block"
    assert (
        "comparison_total_metrics_missing"
        in decision.payload["reason_codes"]
    )


def test_failclosed_contract_version_mismatch_blocks_in_control():
    """Red-team Pass 1 #1 defence-in-depth: a comparison artifact whose
    alignment_contract_version drifts from the binding file is blocked
    by control even though it never went through the comparator."""
    cmp = _comparison(0.95, {"decision": _pt(0.95, 5)})
    cmp.payload["alignment_contract_version"] = "9.9.9"
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "block"
    assert (
        "comparison_contract_version_mismatch"
        in decision.payload["reason_codes"]
    )


def test_required_fields_eval_blocks_malformed_comparison():
    """The runner's required-fields gate for the new type: a comparison
    missing per_type_metrics fails an eval -> decide_control blocks
    (gate covered by its own rejection test, red-team Pass 2)."""
    cmp = _comparison(0.95, {"decision": _pt(0.95, 5)})
    del cmp.payload["per_type_metrics"]
    results = run_required_evals(cmp)
    assert any(r.payload["status"] == "fail" for r in results)
    decision = decide_control(cmp, results)
    assert decision.payload["decision"] == "block"


def test_tiny_sample_not_falsely_blocked():
    """decision f1 0.0 but ceiling_count 2 (< 3) -> the count floor
    suppresses the per-type alarm; total 0.80 passes -> allow."""
    cmp = _comparison(0.80, {"decision": _pt(0.0, 2)})
    decision = decide_control(cmp, [_passing_eval(cmp)])
    assert decision.payload["decision"] == "allow"
