"""Schema-level validation tests — Phase I (CHECK-RT1-001..005)."""
from __future__ import annotations

import unittest

from spectrum_systems_core.governance._schema import (
    validate_governance_artifact,
)


def _baseline_audit_record() -> dict:
    return {
        "audit_id": "00000000-0000-4000-8000-000000000000",
        "audit_type": "schema_drift",
        "scope": "system_wide",
        "generated_at": "2026-05-09T00:00:00+00:00",
        "current_value": {"total_schemas": 0},
        "prior_value": None,
        "delta": None,
        "flagged_items": [],
        "total_scanned": 0,
        "total_flagged": 0,
        "status": "clean",
    }


def _baseline_compression_candidate() -> dict:
    return {
        "candidate_id": "00000000-0000-4000-8000-000000000000",
        "candidate_type": "class",
        "candidate_path": "src/x.py",
        "candidate_name": "X",
        "reason": "lonely class with no users",
        "evidence": {},
        "recommended_action": "investigate",
        "status": "proposed",
        "proposed_at": "2026-05-09T00:00:00+00:00",
        "applied_at": None,
        "applied_by": None,
        "applied_action_detail": "",
    }


class SchemaValidationTests(unittest.TestCase):
    def test_rt1_001_first_audit_prior_value_null_is_valid(self) -> None:
        record = _baseline_audit_record()
        ok, err = validate_governance_artifact(
            record, "governance_audit_record"
        )
        self.assertTrue(ok, err)

    def test_rt1_002_compression_action_must_be_in_enum(self) -> None:
        bad = _baseline_compression_candidate()
        bad["recommended_action"] = "shred"
        ok, _ = validate_governance_artifact(bad, "compression_candidate")
        self.assertFalse(ok)

    def test_rt1_004_short_recommended_action_rejected(self) -> None:
        record = _baseline_audit_record()
        record["flagged_items"] = [
            {
                "item_type": "x",
                "item_id": "y",
                "detail": "this is a long enough detail",
                "severity": "low",
                "recommended_action": "fix",
            }
        ]
        record["total_flagged"] = 1
        ok, _ = validate_governance_artifact(
            record, "governance_audit_record"
        )
        self.assertFalse(ok)

    def test_rt1_005_audit_type_outside_enum_rejected(self) -> None:
        bad = _baseline_audit_record()
        bad["audit_type"] = "something_made_up"
        ok, _ = validate_governance_artifact(
            bad, "governance_audit_record"
        )
        self.assertFalse(ok)

    def test_rt1_001_status_can_be_insufficient_history(self) -> None:
        record = _baseline_audit_record()
        record["status"] = "insufficient_history"
        ok, err = validate_governance_artifact(
            record, "governance_audit_record"
        )
        self.assertTrue(ok, err)

    def test_audit_record_status_outside_enum_rejected(self) -> None:
        record = _baseline_audit_record()
        record["status"] = "not_a_status"
        ok, _ = validate_governance_artifact(
            record, "governance_audit_record"
        )
        self.assertFalse(ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
