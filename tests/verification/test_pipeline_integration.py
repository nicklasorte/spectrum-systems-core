"""Phase V — pipeline-wiring tests (apply_phase_v_if_enabled)."""
from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.config.feature_flag import PHASE_V_FLAG_NAME
from spectrum_systems_core.verification.pipeline_integration import (
    VerificationIncompleteError,
    apply_phase_v_if_enabled,
)


def _enable_flag(root: pathlib.Path, enabled: bool = True) -> None:
    d = root / "store" / "artifacts" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": bool(enabled)}), encoding="utf-8",
    )


def _claim(text="c", turn_ids=("t-1",)):
    return {
        "claim_text": text,
        "claim_type": "technical",
        "speaker": "Alice",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }


def _meeting_extraction(claims):
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "decisions": [],
        "claims": list(claims),
        "action_items": [],
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


def _chunks(turn_ids=("t-1",)):
    return {
        tid: {"chunk_id": tid, "text": "DoD agreed.", "speaker": "Alice", "timestamp": "00:00"}
        for tid in turn_ids
    }


def test_pipeline_skips_verification_when_flag_disabled(tmp_path):
    _enable_flag(tmp_path, enabled=False)
    extraction = _meeting_extraction([_claim()])
    caller_calls: List[str] = []
    sdl_root = tmp_path / "sdl"
    result = apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path,
        sdl_root=sdl_root,
        api_caller=lambda p: (caller_calls.append(p), {})[1],
    )
    assert result is None
    # No verification artifact written (assert unconditionally; the dir
    # may or may not exist, but it must contain zero files either way).
    verif_dir = sdl_root / "verifications"
    assert not verif_dir.exists() or list(verif_dir.glob("*.json")) == []
    # No model calls
    assert caller_calls == []
    # Schema not bumped
    assert extraction["schema_version"] == "1.1.0"
    # No verification_status annotations
    assert "verification_status" not in extraction["claims"][0]


def test_pipeline_writes_verification_artifact_when_flag_enabled(tmp_path):
    _enable_flag(tmp_path, enabled=True)
    sdl_root = tmp_path / "sdl"
    extraction = _meeting_extraction([_claim()])

    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["DoD agreed."],
        "verifier_confidence": 0.9,
        "verifier_rationale": "match.",
    }
    result = apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path,
        sdl_root=sdl_root,
        api_caller=caller,
    )

    assert result is not None
    files = list((sdl_root / "verifications").glob("*_source_verification_result.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["artifact_type"] == "source_verification_result"
    assert written["summary"]["verified_count"] == 1


def test_each_meeting_extraction_item_has_verification_status_when_enabled(tmp_path):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    extraction = _meeting_extraction([
        _claim("c1", ["t-1"]),
        _claim("c2", ["t-1"]),
    ])

    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["DoD agreed."],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(), data_lake_path=tmp_path,
        sdl_root=sdl_root, api_caller=caller,
    )

    assert extraction["schema_version"] == "2.0.0"
    assert extraction["verification_artifact_id"]
    for c in extraction["claims"]:
        assert c["verification_status"] == "verified"


def test_pipeline_annotates_unsupported_items_with_exclusion_reason(tmp_path):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    extraction = _meeting_extraction([_claim()])

    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9,
        "verifier_rationale": "no.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(), data_lake_path=tmp_path,
        sdl_root=sdl_root, api_caller=caller,
    )

    item = extraction["claims"][0]
    assert item["verification_status"] == "unsupported"
    assert "post_hoc_unsupported" in item["exclusion_reasons"]
    assert item["items_requiring_review"] is True
    assert item["review_reason"] == "post_hoc_unsupported"


def test_pipeline_preserves_low_confidence_reason_alongside_post_hoc(tmp_path):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    item = _claim()
    item["items_requiring_review"] = True
    item["review_reason"] = "low_confidence"
    extraction = _meeting_extraction([item])

    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9,
        "verifier_rationale": "no.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(), data_lake_path=tmp_path,
        sdl_root=sdl_root, api_caller=caller,
    )
    out = extraction["claims"][0]
    reasons = out["exclusion_reasons"]
    assert "low_confidence" in reasons
    assert "post_hoc_unsupported" in reasons


def test_pipeline_halts_when_verification_halted(tmp_path):
    """A halted sanity check writes the verification artifact and
    leaves verification_failed status on items past the halt point.
    The completeness check is bypassed because halted runs are
    expected to be partial.
    """
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    claims = [_claim(f"c{i}", ["t-1"]) for i in range(10)]
    extraction = _meeting_extraction(claims)

    caller = lambda p: {  # noqa: E731
        "verification_status": "unsupported",
        "supporting_text_excerpts": [],
        "verifier_confidence": 0.9,
        "verifier_rationale": "no.",
    }
    result = apply_phase_v_if_enabled(
        extraction, _chunks(), data_lake_path=tmp_path,
        sdl_root=sdl_root, api_caller=caller,
    )

    assert result["summary"]["status"] == "halted_sanity_check"

    # Items past halt point fall through to verification_failed status.
    failed = sum(
        1 for c in extraction["claims"]
        if c["verification_status"] == "verification_failed"
    )
    assert failed > 0

    # The verification artifact must be on disk so HITL operators can
    # inspect it -- a halted run still produces a record.
    files = list((sdl_root / "verifications").glob("*_source_verification_result.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    # The halt status is persisted (not just present in the in-memory dict).
    assert written["summary"]["status"] == "halted_sanity_check"


def test_data_lake_path_with_no_sdl_writes_under_sdl_root(tmp_path):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "elsewhere"
    extraction = _meeting_extraction([_claim()])
    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["DoD agreed."],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok.",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(),
        data_lake_path=tmp_path,
        sdl_root=sdl_root,
        api_caller=caller,
    )
    assert (sdl_root / "verifications").is_dir()
    assert list((sdl_root / "verifications").glob("*.json"))


def test_pipeline_provenance_bumped_to_phase_v(tmp_path):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    extraction = _meeting_extraction([_claim()])
    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["DoD agreed."],
        "verifier_confidence": 0.9,
        "verifier_rationale": "ok",
    }
    apply_phase_v_if_enabled(
        extraction, _chunks(), data_lake_path=tmp_path,
        sdl_root=sdl_root, api_caller=caller,
    )
    assert extraction["provenance"]["phase"] == "V"


def test_pipeline_incomplete_raises_when_verifier_returns_too_few(tmp_path, monkeypatch):
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "sdl"
    extraction = _meeting_extraction([_claim("c1", ["t-1"]), _claim("c2", ["t-1"])])

    # Patch PostHocVerifier.verify_extraction to return a dict with only
    # one item_verification. Simulates a bug that would silently lose
    # items.
    from spectrum_systems_core.verification import pipeline_integration as pi

    def _faulty_verify(self, art, chunks, pipeline_run_id, *, trace_id=None):
        return {
            "source_verification_result_id": str(uuid.uuid4()),
            "artifact_type": "source_verification_result",
            "schema_version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "trace_id": None,
            "pipeline_run_id": str(pipeline_run_id),
            "meeting_extraction_artifact_id": art["meeting_extraction_id"],
            "source_id": "test",
            "item_verifications": [],  # zero entries
            "summary": {
                "total_items_count": 0, "verified_count": 0,
                "unsupported_count": 0, "contradicted_count": 0,
                "insufficient_evidence_count": 0,
                "verification_failed_count": 0,
                "spurious_add_rate": 0.0, "status": "complete",
            },
            "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
        }

    monkeypatch.setattr(pi.PostHocVerifier, "verify_extraction", _faulty_verify)

    with pytest.raises(VerificationIncompleteError):
        apply_phase_v_if_enabled(
            extraction, _chunks(),
            data_lake_path=tmp_path,
            sdl_root=sdl_root,
            api_caller=lambda p: {},
        )


def test_run_typed_extraction_returns_failure_on_incomplete_verification(tmp_path, monkeypatch):
    """End-to-end: the runner must NOT write a meeting_extraction file
    when Phase V verification is incomplete.
    """
    # Wire up minimal data lake to satisfy run_typed_extraction's path
    # discovery. We monkeypatch the inner classifier+extractor calls
    # via injected api_callers, then force the verifier to return zero
    # entries (the incomplete bug).
    _enable_flag(tmp_path)
    sdl_root = tmp_path / "store" / "artifacts"
    # Create chunks.jsonl for a fake meeting.
    source_id = "test-meeting"
    proc_dir = tmp_path / "store" / "processed" / "meetings" / source_id
    (proc_dir / "stories").mkdir(parents=True)
    chunks_path = proc_dir / "stories" / "chunks.jsonl"
    chunks_path.write_text(
        json.dumps({"chunk_id": "t-1", "text": "We agreed."}) + "\n",
        encoding="utf-8",
    )
    # Write a source_record so source_artifact_id is stable.
    source_artifact_id = str(uuid.uuid4())
    (proc_dir / "source_record.json").write_text(
        json.dumps({"artifact_id": source_artifact_id}), encoding="utf-8",
    )

    from spectrum_systems_core.extraction import typed_extraction_runner as tr
    from spectrum_systems_core.verification import pipeline_integration as pi

    # Disable real anthropic SDK lookups.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))

    # Force a single claim into the merged artifact so completeness
    # check has something to count.
    def _fake_merge(self, **kwargs):
        return {
            "meeting_extraction_id": str(uuid.uuid4()),
            "source_artifact_id": kwargs["source_artifact_id"],
            "artifact_type": "meeting_extraction",
            "schema_version": "1.1.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "decisions": [],
            "claims": [_claim("c", ["t-1"])],
            "action_items": [],
            "total_chunks_classified": 1,
            "off_topic_count": 0,
            "regulatory_verb_fallback_count": 0,
            "routing_quality_warning": False,
            "requires_human_dedup_count": 0,
            "extraction_run_id": kwargs["extraction_run_id"],
            "few_shot_injected": False,
            "few_shot_version": None,
            "few_shot_example_count": 0,
            "omit_instruction_present": True,
            "confidence_threshold": 0.5,
            "low_confidence_item_count": 0,
            "provenance": {"produced_by": "ExtractionMerger"},
        }

    monkeypatch.setattr(
        "spectrum_systems_core.extraction.extraction_merger.ExtractionMerger.merge",
        _fake_merge,
    )

    # Faulty verify_extraction -> zero entries.
    def _faulty_verify(self, art, chunks, pipeline_run_id, *, trace_id=None):
        return {
            "source_verification_result_id": str(uuid.uuid4()),
            "artifact_type": "source_verification_result",
            "schema_version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "trace_id": None,
            "pipeline_run_id": str(pipeline_run_id),
            "meeting_extraction_artifact_id": art["meeting_extraction_id"],
            "source_id": "test",
            "item_verifications": [],
            "summary": {
                "total_items_count": 0, "verified_count": 0,
                "unsupported_count": 0, "contradicted_count": 0,
                "insufficient_evidence_count": 0,
                "verification_failed_count": 0,
                "spurious_add_rate": 0.0, "status": "complete",
            },
            "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
        }

    monkeypatch.setattr(pi.PostHocVerifier, "verify_extraction", _faulty_verify)

    result = tr.run_typed_extraction(source_id, data_lake=str(tmp_path), force=True)
    assert result["status"] == "failure"
    assert "verification_incomplete" in result["reason"]
    # The meeting_extraction file must not have been written.
    out_path = sdl_root / "extractions" / f"{source_artifact_id}_meeting_extraction.json"
    assert not out_path.exists()
