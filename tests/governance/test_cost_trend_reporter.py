"""Tests for CostTrendReporter — Phase I."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import CostTrendReporter

from ._fixtures import (
    make_run_entry,
    stage_minimal_repo,
    write_run_history,
)


class CostTrendReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_insufficient_history_with_empty(self) -> None:
        write_run_history(self.repo_root, [])
        result = CostTrendReporter().scan(self.repo_root)
        self.assertEqual(result["status"], "insufficient_history")
        self.assertIsNone(result["current_value"]["current_30d_total"])
        self.assertIsNone(result["current_value"]["delta_pct"])

    def test_insufficient_history_short_window(self) -> None:
        runs = [
            make_run_entry(cost_usd=0.5, days_ago=5),
            make_run_entry(cost_usd=0.5, days_ago=10),
        ]
        write_run_history(self.repo_root, runs)
        result = CostTrendReporter().scan(self.repo_root)
        self.assertEqual(result["status"], "insufficient_history")

    def test_increase_flagged_degrading_high(self) -> None:
        runs = [
            make_run_entry(cost_usd=10.0, days_ago=5),
            make_run_entry(cost_usd=5.0, days_ago=10),
            make_run_entry(cost_usd=2.0, days_ago=40),
            make_run_entry(cost_usd=1.0, days_ago=50),
            make_run_entry(cost_usd=0.5, days_ago=70),
        ]
        write_run_history(self.repo_root, runs)
        result = CostTrendReporter().scan(self.repo_root)
        self.assertEqual(
            result["current_value"]["status"],
            "degrading",
        )
        # Current=15.0 prior=3.0 -> delta_pct=400% -> high
        self.assertTrue(result["flagged_items"])
        self.assertEqual(result["flagged_items"][0]["severity"], "high")

    def test_decrease_flagged_improving(self) -> None:
        runs = [
            make_run_entry(cost_usd=0.1, days_ago=5),
            make_run_entry(cost_usd=0.1, days_ago=10),
            make_run_entry(cost_usd=10.0, days_ago=40),
            make_run_entry(cost_usd=10.0, days_ago=50),
            make_run_entry(cost_usd=0.5, days_ago=65),
        ]
        write_run_history(self.repo_root, runs)
        result = CostTrendReporter().scan(self.repo_root)
        self.assertEqual(
            result["current_value"]["status"],
            "improving",
        )

    def test_stable_within_band(self) -> None:
        runs = [
            make_run_entry(cost_usd=1.0, days_ago=5),
            make_run_entry(cost_usd=1.0, days_ago=15),
            make_run_entry(cost_usd=1.0, days_ago=40),
            make_run_entry(cost_usd=1.05, days_ago=50),
            make_run_entry(cost_usd=0.5, days_ago=65),
        ]
        write_run_history(self.repo_root, runs)
        result = CostTrendReporter().scan(self.repo_root)
        self.assertEqual(
            result["current_value"]["status"],
            "stable",
        )
        self.assertEqual(result["total_flagged"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
