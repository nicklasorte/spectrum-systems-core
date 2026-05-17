"""Phase T.5 tests: per-entity-type F1 in eval_summary."""
from __future__ import annotations

import unittest
from typing import Any

from spectrum_systems_core.evals.m4.runner import EvalRunner


class _StubEvalRunner(EvalRunner):
    """Bypass file I/O; we only test _compute_per_type_metrics here."""

    def __init__(self) -> None:
        self.sdl_root = None
        self.pipeline_run_id = "test-run"


class PerTypeMetricsTests(unittest.TestCase):

    def _eval_result(self, coverage: float, precision: float) -> dict[str, Any]:
        return {
            "coverage": coverage,
            "precision": precision,
            "eval_result_id": "er-x",
        }

    def test_per_type_metrics_when_target_type_present(self) -> None:
        runner = _StubEvalRunner()
        eval_results = [
            self._eval_result(coverage=0.8, precision=0.9),
            self._eval_result(coverage=0.4, precision=0.5),
        ]
        pairs = [
            {"pair_id": "p1", "target_type": "decision"},
            {"pair_id": "p2", "target_type": "claim"},
        ]
        metrics, reason = runner._compute_per_type_metrics(eval_results, pairs)
        self.assertIsNotNone(metrics)
        self.assertIsNone(reason)
        self.assertIn("decision", metrics)
        self.assertIn("claim", metrics)
        # F1 is the harmonic mean of (precision, coverage).
        dec = metrics["decision"]
        self.assertAlmostEqual(dec["precision"], 0.9, places=4)
        self.assertAlmostEqual(dec["recall"], 0.8, places=4)
        self.assertGreater(dec["f1"], 0.0)
        self.assertEqual(dec["pairs_count"], 1)

    def test_null_when_any_pair_missing_target_type(self) -> None:
        runner = _StubEvalRunner()
        eval_results = [self._eval_result(0.5, 0.5), self._eval_result(0.5, 0.5)]
        pairs = [
            {"pair_id": "p1", "target_type": "decision"},
            {"pair_id": "p2"},  # missing target_type
        ]
        metrics, reason = runner._compute_per_type_metrics(eval_results, pairs)
        # RT pass 2: null + reason (non-empty string), not empty dict.
        self.assertIsNone(metrics)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)

    def test_zero_pair_bucket_emits_zero_metrics(self) -> None:
        runner = _StubEvalRunner()
        eval_results = [self._eval_result(0.8, 0.9)]
        pairs = [{"pair_id": "p1", "target_type": "decision"}]
        metrics, reason = runner._compute_per_type_metrics(eval_results, pairs)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["claim"]["pairs_count"], 0)
        self.assertEqual(metrics["action_item"]["pairs_count"], 0)
        self.assertEqual(metrics["claim"]["f1"], 0.0)

    def test_invalid_target_type_treated_as_missing(self) -> None:
        runner = _StubEvalRunner()
        eval_results = [self._eval_result(0.5, 0.5)]
        pairs = [{"pair_id": "p1", "target_type": "not-a-real-type"}]
        metrics, reason = runner._compute_per_type_metrics(eval_results, pairs)
        self.assertIsNone(metrics)
        self.assertIsInstance(reason, str)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
