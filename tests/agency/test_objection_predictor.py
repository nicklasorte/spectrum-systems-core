"""Tests for ObjectionPredictor (FINDING-E-001, FINDING-E-007)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.objection_predictor import ObjectionPredictor
from spectrum_systems_core.agency.profile_store import AgencyProfileStore

from ._fixtures import (
    make_claim,
    make_position_entry,
    read_jsonl,
    write_paper_claims,
)


def _ok_response(positions_referenced: list[str]) -> str:
    return json.dumps(
        {
            "predicted_objection_text": (
                "FCC will likely contend that the methodology "
                "underestimates adjacent-channel interference effects."
            ),
            "objection_type": "methodology_concern",
            "confidence": "high",
            "rationale": "Aligns with their long-standing position on adjacent channel.",
            "positions_referenced": positions_referenced,
        }
    )


def _no_history_response() -> str:
    return json.dumps({"insufficient_history": True})


class ObjectionPredictorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "paper-A"
        self.family = "working_papers"

        write_paper_claims(
            self.repo_root,
            family=self.family,
            paper_source_id=self.paper_id,
            claims=[
                make_claim(
                    claim_text="The proposed allocation reduces interference by 30%.",
                ),
                make_claim(
                    claim_text="The methodology accounts for adjacent channel effects.",
                ),
            ],
        )

        self.store = AgencyProfileStore()
        self.store.get_or_create("FCC", str(self.repo_root))
        self.position = make_position_entry(
            agency_slug="fcc",
            topic="adjacent channel interference",
            statement=(
                "The FCC has consistently raised concerns about adjacent "
                "channel interference modelling."
            ),
        )
        result = self.store.add_position(
            "fcc", self.position, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prediction_with_evidence_basis(self) -> None:
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["adjacent channel interference"])
        )
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        prediction = result["predictions"][0]
        self.assertGreater(len(prediction["evidence_basis"]), 0)
        self.assertFalse(prediction["no_evidence_basis_flag"])
        self.assertEqual(prediction["confidence"], "high")

    def test_empty_evidence_basis_forces_low_confidence(self) -> None:
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["unrelated topic"])
        )
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        prediction = result["predictions"][0]
        self.assertEqual(prediction["evidence_basis"], [])
        self.assertTrue(prediction["no_evidence_basis_flag"])
        self.assertEqual(prediction["confidence"], "low")

    def test_no_active_positions_returns_insufficient_history(self) -> None:
        # Wipe positions.
        positions_path = (
            self.repo_root / "agency" / "fcc" / "positions.jsonl"
        )
        positions_path.write_text("")
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["x"])
        )
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "insufficient_history")
        # predictions.jsonl NOT written.
        self.assertFalse(
            (
                self.repo_root
                / "processed"
                / self.family
                / self.paper_id
                / "paper"
                / "objections"
                / "predictions.jsonl"
            ).is_file()
        )

    def test_recency_cutoff_applied_flag_always_true(self) -> None:
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["adjacent channel interference"])
        )
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["predictions"][0]["recency_cutoff_applied"])

    def test_api_failure_returns_failure_not_crash(self) -> None:
        def _raises(_prompt: str) -> str:
            raise RuntimeError("api down")

        predictor = ObjectionPredictor(api_caller=_raises)
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("api_error", result["reason"])
        # No predictions.jsonl written.
        self.assertFalse(
            (
                self.repo_root
                / "processed"
                / self.family
                / self.paper_id
                / "paper"
                / "objections"
                / "predictions.jsonl"
            ).is_file()
        )

    def test_predictions_jsonl_overwritten_not_appended(self) -> None:
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["adjacent channel interference"])
        )
        predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        # Run again.
        predictor2 = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["adjacent channel interference"])
        )
        predictor2.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        records = read_jsonl(
            self.repo_root
            / "processed"
            / self.family
            / self.paper_id
            / "paper"
            / "objections"
            / "predictions.jsonl"
        )
        self.assertEqual(len(records), 1)

    def test_evidence_basis_contains_position_ids(self) -> None:
        predictor = ObjectionPredictor(
            api_caller=lambda _p: _ok_response(["adjacent channel interference"])
        )
        result = predictor.predict_for_paper(
            self.paper_id, "fcc", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        evidence = result["predictions"][0]["evidence_basis"]
        self.assertIn(self.position["position_id"], evidence)


if __name__ == "__main__":
    unittest.main()
