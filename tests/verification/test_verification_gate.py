"""Phase V — VerificationGate (Gate-2) tests.

Each rejection test constructs an actual bad input and asserts on
``GateDecision.passed=False`` plus the specific reason code so the
gate's fail-closed contract is observable at the unit level.
"""
from __future__ import annotations

import json
import pathlib
import uuid

from spectrum_systems_core.config.feature_flag import PHASE_V_FLAG_NAME
from spectrum_systems_core.verification.post_hoc_verifier import _coerce_item_id
from spectrum_systems_core.verification.verification_gate import (
    GateDecision,
    VerificationGate,
)


def _enable_flag(data_lake: pathlib.Path, enabled: bool = True) -> None:
    d = data_lake / "store" / "artifacts" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": bool(enabled)}), encoding="utf-8",
    )


def _claim(text: str, turn_ids):
    return {
        "claim_text": text,
        "claim_type": "regulatory",
        "speaker": "X",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }


def _make_extraction(claim_texts=("c1",), turn_ids=("t-1",)):
    claims = [_claim(t, turn_ids) for t in claim_texts]
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "2.0.0",
        "decisions": [],
        "claims": claims,
        "action_items": [],
    }


def _make_verification(extraction, statuses):
    """Build a verification_result for ``extraction`` with the given
    per-claim statuses (in order). statuses==None means "omit entry
    (incomplete coverage)".
    """
    item_verifications = []
    for item, status in zip(extraction["claims"], statuses):
        if status is None:
            continue
        item_verifications.append({
            "item_id": _coerce_item_id(item),
            "item_type": "claim",
            "original_item_text": item["claim_text"],
            "cited_source_turn_ids": list(item["source_turn_ids"]),
            "verification_status": status,
            "supporting_text_excerpts": ["x"] if status == "verified" else [],
            "verifier_confidence": 0.9,
            "verifier_rationale": "",
            "verifier_model_version": "test@1",
            "verified_at": "2026-05-12T00:00:00+00:00",
        })
    return {
        "item_verifications": item_verifications,
        "summary": {
            "total_items_count": len(item_verifications),
            "verified_count": sum(1 for s in statuses if s == "verified"),
            "unsupported_count": sum(1 for s in statuses if s == "unsupported"),
            "contradicted_count": sum(1 for s in statuses if s == "contradicted"),
            "insufficient_evidence_count": sum(1 for s in statuses if s == "insufficient_evidence"),
            "verification_failed_count": sum(1 for s in statuses if s == "verification_failed"),
            "spurious_add_rate": 0.0,
            "status": "complete",
        },
    }


def test_gate_passes_when_phase_v_disabled(tmp_path):
    _enable_flag(tmp_path, enabled=False)
    extraction = _make_extraction(("c1",))
    verification = _make_verification(extraction, ["unsupported"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is True
    assert decision.reason == "phase_v_disabled"


def test_gate_passes_when_flag_file_missing(tmp_path):
    # No flag file written -> FeatureFlag fail-closed to disabled,
    # which means gate short-circuits to pass. The pipeline never
    # invokes the gate with a bad verification artifact in this state.
    extraction = _make_extraction(("c1",))
    decision = VerificationGate().check_phase_v_verification(
        extraction, None, tmp_path,
    )
    assert decision.passed is True
    assert decision.reason == "phase_v_disabled"


def test_gate_passes_when_all_items_verified(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1", "c2"))
    verification = _make_verification(extraction, ["verified", "verified"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is True
    assert decision.reason == "all_items_verified"


def test_gate_blocks_when_verification_result_missing(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1",))
    decision = VerificationGate().check_phase_v_verification(
        extraction, None, tmp_path,
    )
    assert decision.passed is False
    assert decision.reason == "verification_result_missing"
    assert decision.stage == "phase_v_stage_1_completeness"


def test_gate_blocks_on_incomplete_verification(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1", "c2"))
    # Only one of two items got verified.
    verification = _make_verification(extraction, ["verified", None])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.reason == "verification_incomplete"
    assert decision.stage == "phase_v_stage_1_completeness"
    assert decision.details["missing_item_ids"]


def test_gate_blocks_on_unsupported_item(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1",))
    verification = _make_verification(extraction, ["unsupported"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.reason == "items_failed_verification"
    assert decision.stage == "phase_v_stage_2_status"
    assert decision.details["failed_statuses_breakdown"]["unsupported"] == 1


def test_gate_blocks_on_contradicted_item(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1",))
    verification = _make_verification(extraction, ["contradicted"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.details["failed_statuses_breakdown"]["contradicted"] == 1


def test_gate_blocks_on_insufficient_evidence(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1",))
    verification = _make_verification(extraction, ["insufficient_evidence"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.details["failed_statuses_breakdown"]["insufficient_evidence"] == 1


def test_gate_blocks_on_verification_failed_status(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1",))
    verification = _make_verification(extraction, ["verification_failed"])
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.details["failed_statuses_breakdown"]["verification_failed"] == 1


def test_gate_blocks_on_halted_sanity_check(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1", "c2"))
    verification = _make_verification(extraction, ["unsupported", "unsupported"])
    verification["summary"]["status"] = "halted_sanity_check"
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    assert decision.reason == "verification_halted_sanity_check"


def test_gate_decision_includes_failure_breakdown(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction(("c1", "c2", "c3"))
    verification = _make_verification(
        extraction, ["unsupported", "contradicted", "verified"],
    )
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    breakdown = decision.details["failed_statuses_breakdown"]
    assert breakdown["unsupported"] == 1
    assert breakdown["contradicted"] == 1
    assert breakdown["insufficient_evidence"] == 0
    assert breakdown["verification_failed"] == 0


def test_gate_decision_as_dict_roundtrip():
    d = GateDecision(passed=False, reason="x", stage="s", details={"k": 1})
    payload = d.as_dict()
    assert payload == {
        "passed": False, "reason": "x", "stage": "s", "details": {"k": 1},
    }
