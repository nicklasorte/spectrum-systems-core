"""Phase V — Red Team Pass 1 regressions.

Each test here proves a Sev-1/Sev-2 finding is fixed. They live in a
separate file so the audit trail (finding → test → fix) is legible.
"""
from __future__ import annotations

import json
import pathlib
import uuid

import pytest

from spectrum_systems_core.config.feature_flag import PHASE_V_FLAG_NAME
from spectrum_systems_core.verification._schemas import (
    SchemaValidationError,
    validate_source_verification_result,
)
from spectrum_systems_core.verification.pipeline_integration import (
    VerificationIncompleteError,
    apply_phase_v_if_enabled,
)
from spectrum_systems_core.verification.post_hoc_verifier import (
    _coerce_uuid,
)
from spectrum_systems_core.verification.verification_gate import (
    VerificationGate,
)


def _enable_flag(root: pathlib.Path) -> None:
    d = root / "store" / "artifacts" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8",
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


def _extraction(claims):
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


# ---------------------------------------------------------------------- #
# RT1 finding #1: env-only data_lake bypass
# ---------------------------------------------------------------------- #


def test_rt1_runner_resolves_data_lake_path_from_env_for_phase_v(tmp_path, monkeypatch):
    """The runner must NOT bypass Phase V when caller drives via env."""
    _enable_flag(tmp_path)
    # Build a minimal data lake.
    source_id = "env-source"
    proc = tmp_path / "store" / "processed" / "meetings" / source_id
    (proc / "stories").mkdir(parents=True)
    (proc / "stories" / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "t-1", "text": "agreed."}) + "\n",
        encoding="utf-8",
    )
    sa_id = str(uuid.uuid4())
    (proc / "source_record.json").write_text(
        json.dumps({"artifact_id": sa_id}), encoding="utf-8",
    )

    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Force the merger to produce 1 claim.
    def _fake_merge(self, **kwargs):
        return _extraction([_claim("c1")])

    monkeypatch.setattr(
        "spectrum_systems_core.extraction.extraction_merger.ExtractionMerger.merge",
        _fake_merge,
    )
    # Block the verifier path by returning verified (so the run succeeds
    # and we can verify Phase V actually ran).
    from spectrum_systems_core.verification import pipeline_integration as pi

    def _fake_verify(self, art, chunks, pipeline_run_id, *, trace_id=None):
        return {
            "source_verification_result_id": str(uuid.uuid4()),
            "artifact_type": "source_verification_result",
            "schema_version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "trace_id": None,
            "pipeline_run_id": _coerce_uuid(pipeline_run_id),
            "meeting_extraction_artifact_id": _coerce_uuid(art["meeting_extraction_id"]),
            "source_id": "env-source",
            "item_verifications": [{
                "item_id": _coerce_uuid_for_claim(art),
                "item_type": "claim",
                "original_item_text": "c1",
                "cited_source_turn_ids": ["t-1"],
                "verification_status": "verified",
                "supporting_text_excerpts": ["agreed."],
                "verifier_confidence": 0.9,
                "verifier_rationale": "ok",
                "verifier_model_version": "fake@1",
                "verified_at": "2026-05-12T00:00:00+00:00",
            }],
            "summary": {
                "total_items_count": 1, "verified_count": 1,
                "unsupported_count": 0, "contradicted_count": 0,
                "insufficient_evidence_count": 0,
                "verification_failed_count": 0,
                "spurious_add_rate": 0.0, "status": "complete",
            },
            "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
        }

    monkeypatch.setattr(pi.PostHocVerifier, "verify_extraction", _fake_verify)

    from spectrum_systems_core.extraction import typed_extraction_runner as tr
    # NOTE: data_lake kwarg deliberately omitted to drive via env only.
    result = tr.run_typed_extraction(source_id, force=True)

    assert result["status"] == "success"
    # A verification artifact must have been written -> Phase V actually ran.
    verifs = list((tmp_path / "store" / "artifacts" / "verifications").glob("*.json"))
    assert len(verifs) == 1


def _coerce_uuid_for_claim(art):
    from spectrum_systems_core.verification.post_hoc_verifier import _coerce_item_id
    return _coerce_item_id(art["claims"][0])


# ---------------------------------------------------------------------- #
# RT1 finding #2: ExtractionMerger.write_to must reject v2 without
# verification_status
# ---------------------------------------------------------------------- #


def test_rt1_write_to_blocks_v2_artifact_missing_verification_status(tmp_path):
    from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger

    artifact = _extraction([_claim("c1")])
    # Simulate the bug: artifact was bumped to v2 but verification_status
    # was never stamped on the item.
    artifact["schema_version"] = "2.0.0"
    artifact["verification_artifact_id"] = str(uuid.uuid4())

    target = tmp_path / "extractions" / f"{uuid.uuid4()}_meeting_extraction.json"
    with pytest.raises(SchemaValidationError):
        ExtractionMerger.write_to(artifact, target)
    assert not target.exists()


def test_rt1_write_to_accepts_legal_v2_artifact(tmp_path):
    from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger

    artifact = _extraction([_claim("c1")])
    artifact["schema_version"] = "2.0.0"
    artifact["verification_artifact_id"] = str(uuid.uuid4())
    artifact["claims"][0]["verification_status"] = "verified"

    target = tmp_path / "extractions" / f"{uuid.uuid4()}_meeting_extraction.json"
    ExtractionMerger.write_to(artifact, target)
    assert target.is_file()


# ---------------------------------------------------------------------- #
# RT1 finding #3: uuid coercion for non-uuid pipeline_run_id/link ids
# ---------------------------------------------------------------------- #


def test_rt1_coerce_uuid_passes_through_real_uuid():
    real = str(uuid.uuid4())
    assert _coerce_uuid(real) == real


def test_rt1_coerce_uuid_stable_for_non_uuid_string():
    a = _coerce_uuid("tex-abc123")
    b = _coerce_uuid("tex-abc123")
    assert a == b  # deterministic
    # Result is uuid-shaped.
    assert uuid.UUID(a)


def test_rt1_verifier_writes_uuid_for_non_uuid_pipeline_run_id(tmp_path):
    _enable_flag(tmp_path)
    sdl = tmp_path / "sdl"
    extraction = _extraction([_claim("c1")])
    caller = lambda p: {  # noqa: E731
        "verification_status": "verified",
        "supporting_text_excerpts": ["agreed."],
        "verifier_confidence": 0.9, "verifier_rationale": "ok",
    }
    result = apply_phase_v_if_enabled(
        extraction, {"t-1": {"chunk_id": "t-1", "text": "agreed.", "speaker": "x", "timestamp": "0"}},
        data_lake_path=tmp_path, sdl_root=sdl,
        pipeline_run_id="tex-not-a-uuid",
        api_caller=caller,
    )
    uuid.UUID(result["pipeline_run_id"])  # raises if not a uuid
    # Validator runs with format_checker; this is implicit but assert it again.
    validate_source_verification_result(result)


# ---------------------------------------------------------------------- #
# RT1 finding #4: halt-with-zero-entries must raise
# ---------------------------------------------------------------------- #


def test_rt1_halt_with_zero_entries_raises(tmp_path, monkeypatch):
    _enable_flag(tmp_path)
    sdl = tmp_path / "sdl"
    extraction = _extraction([_claim(f"c{i}", ["t-1"]) for i in range(6)])

    from spectrum_systems_core.verification import pipeline_integration as pi

    def _zero_halted(self, art, chunks, pipeline_run_id, *, trace_id=None):
        return {
            "source_verification_result_id": str(uuid.uuid4()),
            "artifact_type": "source_verification_result",
            "schema_version": "1.0.0",
            "created_at": "2026-05-12T00:00:00+00:00",
            "trace_id": None,
            "pipeline_run_id": _coerce_uuid(pipeline_run_id),
            "meeting_extraction_artifact_id": _coerce_uuid(art["meeting_extraction_id"]),
            "source_id": "test",
            "item_verifications": [],  # zero entries
            "summary": {
                "total_items_count": 0, "verified_count": 0,
                "unsupported_count": 0, "contradicted_count": 0,
                "insufficient_evidence_count": 0,
                "verification_failed_count": 0,
                "spurious_add_rate": 0.0,
                "status": "halted_sanity_check",
            },
            "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
        }

    monkeypatch.setattr(pi.PostHocVerifier, "verify_extraction", _zero_halted)

    with pytest.raises(VerificationIncompleteError):
        apply_phase_v_if_enabled(
            extraction,
            {"t-1": {"chunk_id": "t-1", "text": "x", "speaker": "", "timestamp": "0"}},
            data_lake_path=tmp_path, sdl_root=sdl,
            api_caller=lambda p: {},
        )


# ---------------------------------------------------------------------- #
# RT1 finding #5: unknown statuses are surfaced in the breakdown
# ---------------------------------------------------------------------- #


def test_rt1_gate_breakdown_surfaces_unknown_status(tmp_path):
    _enable_flag(tmp_path)
    extraction = _extraction([_claim("c1", ["t-1"])])
    from spectrum_systems_core.verification.post_hoc_verifier import _coerce_item_id
    item_id = _coerce_item_id(extraction["claims"][0])
    verification = {
        "item_verifications": [{
            "item_id": item_id,
            "verification_status": "weird_status_not_in_enum",
        }],
        "summary": {
            "total_items_count": 1, "verified_count": 0,
            "unsupported_count": 0, "contradicted_count": 0,
            "insufficient_evidence_count": 0,
            "verification_failed_count": 0,
            "spurious_add_rate": 0.0, "status": "complete",
        },
    }
    decision = VerificationGate().check_phase_v_verification(
        extraction, verification, tmp_path,
    )
    assert decision.passed is False
    breakdown = decision.details["failed_statuses_breakdown"]
    assert breakdown["unknown"] == 1
