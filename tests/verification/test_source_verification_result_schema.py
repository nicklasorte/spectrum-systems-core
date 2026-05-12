"""Phase V — schema validation tests for source_verification_result."""
from __future__ import annotations

import copy
import uuid

import pytest

from spectrum_systems_core.verification._schemas import (
    SchemaValidationError,
    load_schema,
    validate_source_verification_result,
)


_BASE_ITEM = {
    "item_id": str(uuid.uuid4()),
    "item_type": "claim",
    "original_item_text": "DoD agreed to provide updated COA data by Q3.",
    "cited_source_turn_ids": ["t-1"],
    "verification_status": "verified",
    "supporting_text_excerpts": ["DoD agreed to provide COA data by Q3."],
    "verifier_confidence": 0.95,
    "verifier_rationale": "matches verbatim.",
    "verifier_model_version": "claude-sonnet-4-6",
    "verified_at": "2026-05-12T00:00:00+00:00",
}


def _base_artifact(items=None):
    return {
        "source_verification_result_id": str(uuid.uuid4()),
        "artifact_type": "source_verification_result",
        "schema_version": "1.0.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "trace_id": str(uuid.uuid4()),
        "pipeline_run_id": str(uuid.uuid4()),
        "meeting_extraction_artifact_id": str(uuid.uuid4()),
        "source_id": "test-source",
        "item_verifications": items if items is not None else [],
        "summary": {
            "total_items_count": 0,
            "verified_count": 0,
            "unsupported_count": 0,
            "contradicted_count": 0,
            "insufficient_evidence_count": 0,
            "verification_failed_count": 0,
            "spurious_add_rate": 0.0,
            "status": "complete",
        },
        "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
    }


def test_schema_accepts_valid_artifact():
    item = copy.deepcopy(_BASE_ITEM)
    artifact = _base_artifact([item])
    artifact["summary"]["total_items_count"] = 1
    artifact["summary"]["verified_count"] = 1
    validate_source_verification_result(artifact)


def test_schema_rejects_verified_without_excerpts():
    item = copy.deepcopy(_BASE_ITEM)
    item["supporting_text_excerpts"] = []
    item["verification_status"] = "verified"
    artifact = _base_artifact([item])
    artifact["summary"]["total_items_count"] = 1
    artifact["summary"]["verified_count"] = 1
    with pytest.raises(SchemaValidationError):
        validate_source_verification_result(artifact)


def test_schema_accepts_unsupported_with_empty_excerpts():
    item = copy.deepcopy(_BASE_ITEM)
    item["verification_status"] = "unsupported"
    item["supporting_text_excerpts"] = []
    artifact = _base_artifact([item])
    artifact["summary"]["total_items_count"] = 1
    artifact["summary"]["unsupported_count"] = 1
    artifact["summary"]["spurious_add_rate"] = 1.0
    validate_source_verification_result(artifact)


def test_schema_rejects_invalid_status_enum():
    item = copy.deepcopy(_BASE_ITEM)
    item["verification_status"] = "bogus"
    artifact = _base_artifact([item])
    artifact["summary"]["total_items_count"] = 1
    with pytest.raises(SchemaValidationError):
        validate_source_verification_result(artifact)


def test_schema_rejects_missing_required_top_level_field():
    artifact = _base_artifact()
    del artifact["summary"]
    with pytest.raises(SchemaValidationError):
        validate_source_verification_result(artifact)


def test_schema_rejects_cited_source_turn_ids_empty():
    item = copy.deepcopy(_BASE_ITEM)
    item["cited_source_turn_ids"] = []
    artifact = _base_artifact([item])
    artifact["summary"]["total_items_count"] = 1
    with pytest.raises(SchemaValidationError):
        validate_source_verification_result(artifact)


def test_schema_does_not_use_artifact_kind():
    """Pre-N migration: the schema must use artifact_type only."""
    schema = load_schema("source_verification_result")
    assert "artifact_kind" not in schema["properties"]
    assert schema["properties"]["artifact_type"]["const"] == "source_verification_result"
