"""Phase X-3 unit tests: confidence calibration distribution check.

The check must:
  * fire when > 80% of items have confidence > 0.85
  * NOT fire when the distribution is spread
  * exclude blocked-chunk items from the histogram (the inputs are
    drawn from succeeded chunks only)
"""
from __future__ import annotations

import unittest

from spectrum_systems_core.extraction._calibration import (
    HIGH_CONFIDENCE_CUTOFF,
    HIGH_CONFIDENCE_RATE_THRESHOLD,
    calibration_from_succeeded,
    compute_calibration,
)
from spectrum_systems_core.extraction._chunk_counters import ChunkCounters


def _decision(conf: float, n: int = 1):
    return [
        {
            "decision_text": f"d{i}",
            "decision_type": "approved",
            "stakeholders": [],
            "rationale": None,
            "source_turn_ids": ["t1"],
            "source_turn_validation": "verified",
            "confidence": conf,
        }
        for i in range(n)
    ]


class CalibrationFiresWhenSkewedTests(unittest.TestCase):
    def test_fires_when_91_pct_above_cutoff(self) -> None:
        items = _decision(1.0, n=10) + _decision(0.5, n=1)
        out = compute_calibration(items)
        self.assertIsNotNone(out)
        self.assertEqual(out["artifact_type"], "calibration_warning")
        self.assertAlmostEqual(out["high_confidence_rate"], 10 / 11)
        self.assertEqual(out["threshold"], HIGH_CONFIDENCE_RATE_THRESHOLD)
        self.assertEqual(
            out["high_confidence_cutoff"], HIGH_CONFIDENCE_CUTOFF,
        )

    def test_does_not_fire_when_distribution_spread(self) -> None:
        items = (
            _decision(1.0, n=3) + _decision(0.7, n=3) + _decision(0.4, n=4)
        )
        # 3/10 == 0.3 above 0.85 -> below 0.8 threshold -> no warning.
        self.assertIsNone(compute_calibration(items))

    def test_does_not_fire_at_exactly_threshold(self) -> None:
        # 4/5 == 0.8; rule is `> 0.8` -> no warning at exactly threshold.
        items = _decision(0.95, n=4) + _decision(0.5, n=1)
        self.assertIsNone(compute_calibration(items))

    def test_fires_just_above_threshold(self) -> None:
        # 9/10 == 0.9, > 0.8 threshold.
        items = _decision(0.95, n=9) + _decision(0.5, n=1)
        out = compute_calibration(items)
        self.assertIsNotNone(out)

    def test_empty_items_returns_none(self) -> None:
        self.assertIsNone(compute_calibration([]))


class CalibrationExcludesBlockedChunksTests(unittest.TestCase):
    def test_only_succeeded_items_in_histogram(self) -> None:
        # The blocked chunks never produce items -- the items we pass
        # in are by construction the succeeded set. The counter is
        # recorded on the artifact but does NOT affect the rate.
        counters = ChunkCounters()
        counters.record_attempt(20)
        counters.record_success(10)
        counters.record_block("rate_limit_exhausted", n=10)

        items = _decision(0.95, n=9) + _decision(0.5, n=1)
        out = calibration_from_succeeded(
            items, [], [], counters=counters, run_id="tex-x",
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["run_id"], "tex-x")
        # Rate is 9/10 = 0.9, NOT 9/20 = 0.45. The blocked chunks did
        # NOT dilute the denominator (RT1 finding: zero-confidence
        # items from blocked chunks must NOT enter the histogram).
        self.assertAlmostEqual(out["high_confidence_rate"], 0.9)
        self.assertEqual(out["items_total"], 10)
        self.assertEqual(out["items_above_high_confidence"], 9)


class CalibrationArtifactShapeTests(unittest.TestCase):
    def test_artifact_passes_schema_validation(self) -> None:
        from spectrum_systems_core.validation import validate_artifact

        items = _decision(0.95, n=10)
        out = compute_calibration(items)
        assert out is not None
        out["run_id"] = "tex-x"
        validate_artifact(out, "calibration_warning")


class ExtractionItemConfidenceShapeTests(unittest.TestCase):
    """End-to-end confidence field assertions for typed_extraction."""

    def test_extraction_artifact_with_confidence_passes_schema(self) -> None:
        from spectrum_systems_core.validation import validate_artifact

        artifact = {
            "artifact_type": "typed_extraction",
            "schema_version": "1.0.0",
            "source_id": "src",
            "extraction_run_id": "tex-1",
            "decisions": [
                {
                    "decision_text": "approved",
                    "decision_type": "approved",
                    "source_turn_ids": ["t1"],
                    "confidence": 0.9,
                }
            ],
            "claims": [
                {
                    "claim_text": "we say so",
                    "claim_type": "technical",
                    "source_turn_ids": ["t2"],
                    "confidence": 0.7,
                }
            ],
            "action_items": [
                {
                    "action": "do it",
                    "owner": "alice",
                    "source_turn_ids": ["t3"],
                }
            ],
        }
        validate_artifact(artifact, "typed_extraction")

    def test_extraction_artifact_with_invalid_confidence_fails(self) -> None:
        from spectrum_systems_core.validation import (
            ArtifactValidationError,
            validate_artifact,
        )

        artifact = {
            "artifact_type": "typed_extraction",
            "schema_version": "1.0.0",
            "source_id": "src",
            "extraction_run_id": "tex-1",
            "decisions": [
                {
                    "decision_text": "approved",
                    "decision_type": "approved",
                    "source_turn_ids": ["t1"],
                    "confidence": 1.1,
                }
            ],
            "claims": [],
            "action_items": [],
        }
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(artifact, "typed_extraction")


if __name__ == "__main__":
    unittest.main()
