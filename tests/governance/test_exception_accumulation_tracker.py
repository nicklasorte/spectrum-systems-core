"""Tests for ExceptionAccumulationTracker — Phase I."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import ExceptionAccumulationTracker

from ._fixtures import stage_minimal_repo, write_override


class ExceptionAccumulationTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_below_threshold_no_flag(self) -> None:
        for _ in range(3):
            write_override(
                self.repo_root,
                decision_context="cost trend exceeds threshold for paper-1",
                overridden_eval_or_block="cost_trend_block",
            )
        result = ExceptionAccumulationTracker().scan(self.repo_root)
        self.assertEqual(result["total_flagged"], 0)

    def test_above_threshold_flagged(self) -> None:
        for _ in range(6):
            write_override(
                self.repo_root,
                decision_context="cost trend exceeds threshold for paper-1",
                overridden_eval_or_block="cost_trend_block",
            )
        result = ExceptionAccumulationTracker().scan(self.repo_root)
        self.assertGreater(result["total_flagged"], 0)

    def test_high_severity_for_count_over_10(self) -> None:
        for _ in range(11):
            write_override(
                self.repo_root,
                decision_context="grounding eval failed missing citations review",
                overridden_eval_or_block="grounding_block",
            )
        result = ExceptionAccumulationTracker().scan(self.repo_root)
        self.assertTrue(result["flagged_items"])
        self.assertEqual(result["flagged_items"][0]["severity"], "high")

    def test_uses_keyword_grouping(self) -> None:
        # Same first 5 significant words across overrides -> same group.
        for _ in range(6):
            write_override(
                self.repo_root,
                decision_context=(
                    "fairness review override accepted by team for paper "
                    "alpha"
                ),
                overridden_eval_or_block="fairness_block",
            )
        result = ExceptionAccumulationTracker().scan(self.repo_root)
        self.assertGreater(result["total_flagged"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
