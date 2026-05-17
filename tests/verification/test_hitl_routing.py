"""Phase V — HITL queue routing tests.

Same artifact, same items_requiring_review queue, multiple reasons. The
contract is:

* an item that fails post-hoc verification gets ``items_requiring_review
  = True`` and a ``post_hoc_*`` reason appended to ``exclusion_reasons``.
* a previously low-confidence item that ALSO fails post-hoc carries
  BOTH reasons -- never duplicated.
* a verified item is never tagged for review by Phase V (its low-confidence
  flag, if any, is untouched).
"""
from __future__ import annotations

import json
import pathlib
import uuid

from spectrum_systems_core.config.feature_flag import PHASE_V_FLAG_NAME
from spectrum_systems_core.verification.pipeline_integration import (
    apply_phase_v_if_enabled,
)


def _enable_flag(root: pathlib.Path) -> None:
    d = root / "store" / "artifacts" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8",
    )


def _make_extraction(items):
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "decisions": [],
        "claims": list(items),
        "action_items": [],
        "created_at": "2026-05-12T00:00:00+00:00",
        "total_chunks_classified": 1,
        "off_topic_count": 0,
        "regulatory_verb_fallback_count": 0,
        "routing_quality_warning": False,
        "requires_human_dedup_count": 0,
        "extraction_run_id": "tex-test",
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": True,
        "confidence_threshold": 0.5,
        "low_confidence_item_count": 0,
        "provenance": {"produced_by": "ExtractionMerger"},
    }


def _claim(text="c", **kwargs):
    item = {
        "claim_text": text,
        "claim_type": "technical",
        "speaker": "x",
        "source_turn_ids": ["t-1"],
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }
    item.update(kwargs)
    return item


def _chunks():
    return {"t-1": {"chunk_id": "t-1", "text": "<unrelated>", "speaker": "x", "timestamp": "0"}}


def test_unsupported_item_added_to_hitl_queue_with_post_hoc_reason(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction([_claim("c1")])
    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9, "verifier_rationale": "no.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path, sdl_root=tmp_path / "sdl", api_caller=caller,
    )
    item = extraction["claims"][0]
    assert item["items_requiring_review"] is True
    assert item["exclusion_reasons"] == ["post_hoc_unsupported"]


def test_low_confidence_plus_unsupported_carries_both_reasons(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction([
        _claim("c1", items_requiring_review=True, review_reason="low_confidence"),
    ])
    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9, "verifier_rationale": "no.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path, sdl_root=tmp_path / "sdl", api_caller=caller,
    )
    item = extraction["claims"][0]
    reasons = item["exclusion_reasons"]
    assert "low_confidence" in reasons
    assert "post_hoc_unsupported" in reasons
    # No duplication.
    assert reasons.count("low_confidence") == 1
    assert reasons.count("post_hoc_unsupported") == 1


def test_verified_item_not_added_to_hitl_queue(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction([_claim("c1")])
    call_count = {"n": 0}

    def caller(p):
        call_count["n"] += 1
        return {
            "verification_status": "verified",
            "supporting_text_excerpts": ["<unrelated>"],
            "verifier_confidence": 0.9, "verifier_rationale": "ok.",
        }

    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path, sdl_root=tmp_path / "sdl", api_caller=caller,
    )
    item = extraction["claims"][0]
    # Verifier ran (so we're testing the live branch, not the no-op one).
    assert call_count["n"] == 1
    # Verified status was actually stamped.
    assert item["verification_status"] == "verified"
    # No exclusion reasons appended for a verified item.
    assert item.get("exclusion_reasons", []) == []
    # Not tagged for HITL review by Phase V.
    assert item.get("items_requiring_review", False) is False


def test_contradicted_item_uses_contradicted_reason(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction([_claim("c1")])
    caller = lambda p: {  # noqa: E731
        "verification_status": "contradicted",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9, "verifier_rationale": "no.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path, sdl_root=tmp_path / "sdl", api_caller=caller,
    )
    item = extraction["claims"][0]
    assert "post_hoc_contradicted" in item["exclusion_reasons"]


def test_insufficient_evidence_uses_dedicated_reason(tmp_path):
    _enable_flag(tmp_path)
    extraction = _make_extraction([_claim("c1")])
    caller = lambda p: {  # noqa: E731
        "verification_status": "insufficient_evidence",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.4, "verifier_rationale": "vague.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path, sdl_root=tmp_path / "sdl", api_caller=caller,
    )
    item = extraction["claims"][0]
    assert "post_hoc_insufficient_evidence" in item["exclusion_reasons"]
