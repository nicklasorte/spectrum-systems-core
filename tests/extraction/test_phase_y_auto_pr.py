"""Phase Y.7 — auto-PR eligibility predicate (the workflow's brain)."""
from __future__ import annotations

from spectrum_systems_core.extraction.auto_pr_eligibility import (
    evaluate_eligibility,
)


def _ce(target_delta, holdout_delta, regressions=None):
    return {
        "target_delta_f1": target_delta,
        "holdout_delta_f1": holdout_delta,
        "per_type_regressions": regressions or [],
    }


def test_repro_all_conditions_met_eligible():
    r = evaluate_eligibility(_ce(0.06, 0.0, []))
    assert r.eligible is True
    assert r.reasons == []


def test_repro_holdout_minus_001_ineligible():
    r = evaluate_eligibility(_ce(0.10, -0.01, []))
    assert r.eligible is False
    assert "holdout_regression" in r.reasons


def test_target_delta_below_min_ineligible():
    r = evaluate_eligibility(_ce(0.04, 0.10, []))
    assert r.eligible is False
    assert "target_delta_below_min" in r.reasons


def test_per_type_regressions_block():
    r = evaluate_eligibility(
        _ce(0.10, 0.10, [{"transcript_id": "x", "schema_type": "decision",
                          "baseline_f1": 0.9, "candidate_f1": 0.5,
                          "delta": -0.4}])
    )
    assert r.eligible is False
    assert "per_type_regressions_present" in r.reasons


def test_failclosed_malformed_artifact_not_eligible():
    r = evaluate_eligibility({"target_delta_f1": None})
    assert r.eligible is False
    assert r.reasons == ["malformed_candidate_evaluation"]
