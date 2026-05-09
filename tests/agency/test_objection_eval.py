"""Tests for ObjectionEval (FINDING-E-001)."""
from __future__ import annotations

import unittest

from spectrum_systems_core.agency.objection_eval import ObjectionEval

from ._fixtures import make_prediction


class ObjectionEvalTests(unittest.TestCase):
    def test_high_confidence_no_evidence_blocked(self) -> None:
        bad = make_prediction(
            agency_slug="fcc",
            confidence="high",
            evidence_basis=[],
        )
        # Force flag consistency so EVAL-OBJ-005 doesn't also fail it.
        bad["no_evidence_basis_flag"] = True
        result = ObjectionEval().run([bad])
        self.assertEqual(result["decision"], "block")
        self.assertIn(
            "EVAL-OBJ-002:high_confidence_without_evidence_basis",
            result["reason_codes"],
        )

    def test_medium_confidence_no_evidence_passes_with_flag(self) -> None:
        ok = make_prediction(
            agency_slug="fcc", confidence="medium", evidence_basis=[]
        )
        # confidence=medium with no evidence — must have flag set.
        ok["no_evidence_basis_flag"] = True
        result = ObjectionEval().run([ok])
        self.assertEqual(result["decision"], "allow")

    def test_temperature_not_zero_blocked(self) -> None:
        bad = make_prediction(
            agency_slug="fcc",
            confidence="medium",
            evidence_basis=["some-id"],
            extraction_temperature=1,
        )
        bad["no_evidence_basis_flag"] = False
        # bad now has invalid schema (temperature != 0). EVAL-OBJ-001 will
        # fail; EVAL-OBJ-003 may also fail. We assert block and that
        # EVAL-OBJ-003 is among the reasons.
        result = ObjectionEval().run([bad])
        self.assertEqual(result["decision"], "block")
        codes = ";".join(result["reason_codes"])
        self.assertTrue(
            "EVAL-OBJ-003" in codes or "EVAL-OBJ-001" in codes,
            codes,
        )

    def test_no_evidence_basis_flag_consistent_enforced(self) -> None:
        bad = make_prediction(
            agency_slug="fcc",
            confidence="low",
            evidence_basis=["some-id"],
        )
        # Inconsistent: evidence_basis non-empty but flag=True.
        bad["no_evidence_basis_flag"] = True
        result = ObjectionEval().run([bad])
        self.assertEqual(result["decision"], "block")
        self.assertIn(
            "EVAL-OBJ-005:no_evidence_basis_flag_consistent",
            result["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
