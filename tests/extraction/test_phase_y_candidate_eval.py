"""Phase Y.6 — multi-transcript candidate evaluator."""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.extraction.candidate_evaluator import (
    CandidateEvaluatorError,
    evaluate_candidate,
)

TARGET = "m-2025-12-18-7ghz-downlink-tig-kickoff"
HOLDOUT = "m-2025-11-20-ntia-coordination-session"


def _ceiling(_tid):
    return new_artifact(
        artifact_type="opus_ceiling",
        payload={"extracted_items": []},
        trace_id="t",
        status="draft",
    )


def _tagged(total_f1, per_type):
    return new_artifact(
        artifact_type="opus_ceiling",
        payload={"_f1": total_f1, "_pt": per_type, "extracted_items": []},
        trace_id="t",
        status="draft",
    )


def _stub_comparator(_ceiling, haiku, _version):
    return new_artifact(
        artifact_type="extraction_alignment_comparison",
        payload={
            "total_metrics": {
                "recall": 0.0,
                "precision": 0.0,
                "f1": haiku.payload["_f1"],
            },
            "per_type_metrics": {
                st: {
                    "recall": v,
                    "precision": v,
                    "f1": v,
                    "ceiling_count": 5,
                    "haiku_count": 5,
                    "true_positives": 0,
                    "false_negatives": 0,
                    "false_positives": 0,
                }
                for st, v in haiku.payload["_pt"].items()
            },
        },
        trace_id="t",
        status="draft",
    )


def test_repro_holdout_regression_makes_ineligible():
    base = {
        TARGET: _tagged(0.65, {"decision": 0.65}),
        HOLDOUT: _tagged(0.70, {"decision": 0.80}),
    }
    cand = {
        TARGET: _tagged(0.72, {"decision": 0.72}),
        HOLDOUT: _tagged(0.68, {"decision": 0.60}),
    }
    art = evaluate_candidate(
        candidate_id="cand-1",
        candidate_prompt="add rule",
        target_transcript_id=TARGET,
        ceiling_loader=_ceiling,
        baseline_loader=lambda tid: base[tid],
        haiku_runner=lambda tid, _p: cand[tid],
        holdout_transcript_id=HOLDOUT,
        alignment_contract_version="1.0.0",
        comparator=_stub_comparator,
    )
    p = art.payload
    assert p["target_delta_f1"] == pytest.approx(0.07)
    assert p["holdout_delta_f1"] == pytest.approx(-0.02)
    assert p["auto_pr_eligible"] is False
    assert "holdout_regression" in p["eligibility_reason"]
    # Per-type regression on holdout/decision (0.80 -> 0.60).
    regs = p["per_type_regressions"]
    assert any(
        r["transcript_id"] == HOLDOUT and r["schema_type"] == "decision"
        for r in regs
    )


def test_eligible_when_all_conditions_met():
    base = {
        TARGET: _tagged(0.60, {"decision": 0.60}),
        HOLDOUT: _tagged(0.70, {"decision": 0.70}),
    }
    cand = {
        TARGET: _tagged(0.72, {"decision": 0.72}),
        HOLDOUT: _tagged(0.71, {"decision": 0.71}),
    }
    art = evaluate_candidate(
        candidate_id="cand-2",
        candidate_prompt="add rule",
        target_transcript_id=TARGET,
        ceiling_loader=_ceiling,
        baseline_loader=lambda tid: base[tid],
        haiku_runner=lambda tid, _p: cand[tid],
        holdout_transcript_id=HOLDOUT,
        alignment_contract_version="1.0.0",
        comparator=_stub_comparator,
    )
    assert art.payload["auto_pr_eligible"] is True
    assert art.payload["eligibility_reason"] == []


def test_rejection_holdout_not_configured(tmp_path):
    cfg = tmp_path / "phase_y.yaml"
    cfg.write_text("phase_y_target_transcript_id: x\n", encoding="utf-8")
    with pytest.raises(CandidateEvaluatorError) as exc:
        evaluate_candidate(
            candidate_id="c",
            candidate_prompt="p",
            target_transcript_id=TARGET,
            ceiling_loader=_ceiling,
            baseline_loader=lambda tid: _tagged(0.5, {}),
            haiku_runner=lambda tid, _p: _tagged(0.5, {}),
            holdout_transcript_id=None,
            config_path=cfg,
            alignment_contract_version="1.0.0",
            comparator=_stub_comparator,
        )
    assert exc.value.reason_code == "holdout_not_configured"
