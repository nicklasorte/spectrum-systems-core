"""Phase Y.1 — opus_ceiling extractor + ceiling_minimum_counts gate.

Reproduction (per the phase brief) and the fail-closed rejection test
for the new gate.
"""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.evals.runner import run_required_evals
from spectrum_systems_core.extraction.opus_ceiling_extractor import (
    CeilingError,
    extract_ceiling,
)

_SYNTHETIC = (
    "t0001 Chair: Welcome.\n"
    "t0002 Chair: DECISION: the working group approved the 7 GHz "
    "downlink power threshold as proposed.\n"
    "t0003 Staff: noted."
)


def _opus_stub(_text: str):
    return [
        {
            "schema_type": "decision",
            "source_turn_ids": ["t0002"],
            "source_text": "approved the 7 GHz downlink power threshold",
            "payload": {"verb": "approved"},
        }
    ]


def test_repro_extract_ceiling_counts_and_keyword_hits():
    art = extract_ceiling(_SYNTHETIC, "m-y-ceiling-repro", opus_call=_opus_stub)
    assert art.artifact_type == "opus_ceiling"
    assert art.payload["model_id"] == "claude-opus-4-7"
    assert art.payload["per_type_counts"]["decision"] == 1
    # "DECISION:" is in the keyword table -> hit is True deterministically.
    assert art.payload["transcript_keyword_hits"]["decision"] is True
    # A well-formed, non-empty ceiling passes every required eval.
    decision = decide_control(art, run_required_evals(art))
    assert decision.payload["decision"] == "allow"


def test_rejection_ceiling_minimum_counts_blocks_zero_for_keyword_hit():
    """A ceiling with decision count 0 while the transcript visibly
    contains a decision must fail ceiling_minimum_counts with
    `decision` in failed_types — and ONLY that eval (isolation: every
    required field present so no other eval fires)."""
    ceiling = new_artifact(
        artifact_type="opus_ceiling",
        payload={
            "artifact_type": "opus_ceiling",
            "schema_version": "1.0.0",
            "transcript_id": "m-y-ceiling-reject",
            "model_id": "claude-opus-4-7",
            "extracted_items": [
                {
                    "item_id": "x-1",
                    "schema_type": "action_item",
                    "source_turn_ids": ["t1"],
                    "source_text": "follow up",
                    "payload": {},
                }
            ],
            "per_type_counts": {
                "decision": 0,
                "action_item": 1,
                "open_question": 0,
                "claim": 0,
            },
            "transcript_keyword_hits": {
                "decision": True,
                "action_item": True,
                "open_question": False,
                "claim": False,
            },
        },
        trace_id="t-reject",
        status="draft",
    )
    results = run_required_evals(ceiling)
    by_type = {r.payload["eval_type"]: r.payload for r in results}
    assert by_type["ceiling_minimum_counts"]["status"] == "fail"
    assert "decision" in by_type["ceiling_minimum_counts"]["failed_types"]
    # Isolation: it is the ONLY failing eval.
    failed = [p for p in by_type.values() if p["status"] == "fail"]
    assert [p["eval_type"] for p in failed] == ["ceiling_minimum_counts"]
    # And it blocks through the real control path.
    decision = decide_control(ceiling, results)
    assert decision.payload["decision"] == "block"
    assert "failed:ceiling_minimum_counts" in decision.payload["reason_codes"]


def test_failclosed_opus_unavailable_raises_never_empty():
    def _boom(_t):
        raise RuntimeError("network down")

    with pytest.raises(CeilingError) as exc:
        extract_ceiling(_SYNTHETIC, "m-y-x", opus_call=_boom)
    assert exc.value.reason_code == "opus_unavailable"


def test_failclosed_missing_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CeilingError) as exc:
        # No opus_call -> real path -> key check fails closed.
        extract_ceiling(_SYNTHETIC, "m-y-x")
    assert exc.value.reason_code == "opus_unavailable"


def test_invalid_transcript_id_fails_closed():
    with pytest.raises(CeilingError) as exc:
        extract_ceiling(_SYNTHETIC, "Bad Id With Spaces", opus_call=_opus_stub)
    assert exc.value.reason_code == "invalid_transcript_id"
