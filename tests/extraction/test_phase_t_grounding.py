"""Phase T.2 tests: grounding hardening and per-item fields."""
from __future__ import annotations

import os
import unittest
from typing import Any

from spectrum_systems_core.extraction._prompt_blocks import (
    OMIT_INSTRUCTION_BLOCK,
)
from spectrum_systems_core.extraction.source_grounding_verifier import (
    SOURCE_GROUNDING_OVERLAP_THRESHOLD_ENV,
    SPURIOUS_ADD_RATE_BASELINE_BLOCK_ENV,
    compute_spurious_add_rate,
    grounding_overlap_percentiles,
    grounding_threshold,
    spurious_add_baseline_block_threshold,
    verify_extraction_grounding,
)


class OmitBlockPresenceTests(unittest.TestCase):
    """The OMIT instruction must be present in the canonical prompt block."""

    def test_omit_block_says_do_not_infer(self) -> None:
        # Phase T.2: the prompt template's grounding rule must
        # explicitly forbid inference. String-level assertion so a
        # future engineer cannot weaken the rule silently.
        self.assertIn("Do not infer", OMIT_INSTRUCTION_BLOCK)
        self.assertIn("OMIT", OMIT_INSTRUCTION_BLOCK)


class PerItemGroundingFieldsTests(unittest.TestCase):
    """Every item must surface ``grounding_verified`` and ``grounding_overlap_score``."""

    def _chunks(self) -> dict[str, dict[str, Any]]:
        return {
            "c-1": {"chunk_id": "c-1", "text": "FCC approved band plan A-2 for 12.7 GHz."},
            "c-2": {"chunk_id": "c-2", "text": "The committee considered the band plan."},
        }

    def test_high_overlap_item_grounding_verified_true(self) -> None:
        items = [{
            "decision_text": "FCC approved band plan A-2 for 12.7 GHz.",
            "candidate_evidence": "FCC approved band plan A-2 for 12.7 GHz.",
            "source_turn_ids": ["c-1"],
        }]
        summary = verify_extraction_grounding(items, self._chunks(), source_id="s")
        out = summary["annotated_items"]
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["grounding_verified"])
        self.assertGreaterEqual(out[0]["grounding_overlap_score"], 0.4)

    def test_low_overlap_item_grounding_verified_false(self) -> None:
        items = [{
            "decision_text": "Decision text unrelated to source.",
            "candidate_evidence": "completely different unrelated tokens here",
            "source_turn_ids": ["c-1"],
        }]
        summary = verify_extraction_grounding(items, self._chunks(), source_id="s")
        out = summary["annotated_items"]
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["grounding_verified"])
        self.assertLess(out[0]["grounding_overlap_score"], 0.4)

    def test_legacy_grounded_field_still_present(self) -> None:
        # RT pass 2: ``grounding_verified`` is additive. The legacy
        # ``grounded`` field must continue to ship so existing
        # consumers do not break.
        items = [{
            "decision_text": "x",
            "candidate_evidence": "FCC approved band plan A-2.",
            "source_turn_ids": ["c-1"],
        }]
        summary = verify_extraction_grounding(items, self._chunks(), source_id="s")
        out = summary["annotated_items"][0]
        self.assertIn("grounded", out)
        self.assertIn("grounding_verified", out)


class SpuriousAddRateTests(unittest.TestCase):
    """RT pass 2: rate computed only from items the verifier actually scored."""

    def test_rate_3_of_10_is_0_30(self) -> None:
        items = [
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": True},
            {"grounding_verified": False},
            {"grounding_verified": False},
            {"grounding_verified": False},
        ]
        self.assertAlmostEqual(compute_spurious_add_rate(items), 0.30, places=4)

    def test_items_without_field_excluded(self) -> None:
        items = [
            {"grounding_verified": True},
            {"grounding_verified": False},
            {"decision_text": "unverified item"},
        ]
        # 1 false / 2 verdicts = 0.5
        self.assertAlmostEqual(compute_spurious_add_rate(items), 0.5, places=4)


class GroundingThresholdConfigTests(unittest.TestCase):
    """Thresholds are env-tuneable so the operator can change them without code revert."""

    def setUp(self) -> None:
        os.environ.pop(SOURCE_GROUNDING_OVERLAP_THRESHOLD_ENV, None)
        os.environ.pop(SPURIOUS_ADD_RATE_BASELINE_BLOCK_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(SOURCE_GROUNDING_OVERLAP_THRESHOLD_ENV, None)
        os.environ.pop(SPURIOUS_ADD_RATE_BASELINE_BLOCK_ENV, None)

    def test_grounding_threshold_default_0_4(self) -> None:
        self.assertEqual(grounding_threshold(), 0.4)

    def test_grounding_threshold_env_override(self) -> None:
        os.environ[SOURCE_GROUNDING_OVERLAP_THRESHOLD_ENV] = "0.55"
        self.assertEqual(grounding_threshold(), 0.55)

    def test_grounding_threshold_invalid_falls_back(self) -> None:
        os.environ[SOURCE_GROUNDING_OVERLAP_THRESHOLD_ENV] = "junk"
        self.assertEqual(grounding_threshold(), 0.4)

    def test_baseline_block_default_0_25(self) -> None:
        self.assertEqual(spurious_add_baseline_block_threshold(), 0.25)

    def test_baseline_block_env_override(self) -> None:
        os.environ[SPURIOUS_ADD_RATE_BASELINE_BLOCK_ENV] = "0.15"
        self.assertEqual(spurious_add_baseline_block_threshold(), 0.15)


class GroundingPercentilesTests(unittest.TestCase):
    """Percentile helper handles edge cases."""

    def test_empty_input_returns_zeros(self) -> None:
        result = grounding_overlap_percentiles([])
        self.assertEqual(result, {"p10": 0.0, "p50": 0.0, "p90": 0.0})

    def test_single_value_input(self) -> None:
        result = grounding_overlap_percentiles([0.42])
        self.assertEqual(result["p10"], 0.42)
        self.assertEqual(result["p50"], 0.42)
        self.assertEqual(result["p90"], 0.42)

    def test_sorted_distribution(self) -> None:
        result = grounding_overlap_percentiles(
            [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        )
        self.assertLessEqual(result["p10"], result["p50"])
        self.assertLessEqual(result["p50"], result["p90"])
        self.assertLessEqual(result["p90"], 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
