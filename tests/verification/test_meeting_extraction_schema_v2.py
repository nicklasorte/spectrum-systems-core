"""Phase V — meeting_extraction schema v2.0.0 validation."""
from __future__ import annotations

import uuid

import pytest

from spectrum_systems_core.verification._schemas import (
    SchemaValidationError,
    validate_meeting_extraction_v2,
)


def _claim(text="c", turn_ids=("t-1",), *, status=None):
    item = {
        "claim_text": text,
        "claim_type": "technical",
        "speaker": "Alice",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }
    if status is not None:
        item["verification_status"] = status
    return item


def _artifact(version, claims=None, decisions=None, action_items=None):
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": version,
        "decisions": decisions or [],
        "claims": claims or [],
        "action_items": action_items or [],
    }


def test_schema_v1_artifact_validates_without_verification_status():
    """v1 artifacts never carry verification_status; the validator
    must allow them through. v2 rules only kick in at schema_version
    == 2.0.0.
    """
    art = _artifact("1.1.0", claims=[_claim()])
    validate_meeting_extraction_v2(art)  # must not raise


def test_schema_v2_artifact_validates_with_verification_status():
    art = _artifact("2.0.0", claims=[_claim(status="verified")])
    validate_meeting_extraction_v2(art)


def test_schema_v2_artifact_fails_without_verification_status_on_any_item():
    art = _artifact("2.0.0", claims=[
        _claim(status="verified"),
        _claim(text="bad"),  # missing verification_status
    ])
    with pytest.raises(SchemaValidationError):
        validate_meeting_extraction_v2(art)


def test_schema_v2_artifact_fails_with_invalid_status_value():
    art = _artifact("2.0.0", claims=[_claim(status="bogus")])
    with pytest.raises(SchemaValidationError):
        validate_meeting_extraction_v2(art)


def test_schema_v2_covers_decisions_and_action_items():
    art = _artifact("2.0.0",
        decisions=[{
            "decision_text": "d", "decision_type": "approved",
            "stakeholders": [], "rationale": None,
            "source_turn_ids": ["t-1"], "source_turn_validation": "verified",
            "confidence": 0.9,
            # missing verification_status
        }],
    )
    with pytest.raises(SchemaValidationError):
        validate_meeting_extraction_v2(art)


def test_schema_v2_action_item_missing_status_blocks():
    art = _artifact("2.0.0",
        action_items=[{
            "action": "a", "owner": "o", "due": None,
            "source_turn_ids": ["t-1"], "source_turn_validation": "verified",
            "confidence": 0.9,
            # missing verification_status
        }],
    )
    with pytest.raises(SchemaValidationError):
        validate_meeting_extraction_v2(art)


def test_schema_v2_accepts_all_valid_status_values():
    for status in (
        "verified", "unsupported", "contradicted",
        "insufficient_evidence", "verification_failed",
    ):
        art = _artifact("2.0.0", claims=[_claim(status=status)])
        validate_meeting_extraction_v2(art)
