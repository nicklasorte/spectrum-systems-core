"""Tests for DecisionDivergenceDetector — Phase I."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import DecisionDivergenceDetector

from ._fixtures import (
    make_run_entry,
    stage_minimal_repo,
    write_run_history,
)


class DecisionDivergenceDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_consistent_outcomes_no_flags(self) -> None:
        runs = [
            make_run_entry(outcome="success") for _ in range(3)
        ]
        write_run_history(self.repo_root, runs)
        result = DecisionDivergenceDetector().scan(self.repo_root)
        self.assertEqual(result["total_flagged"], 0)
        self.assertEqual(result["status"], "clean")

    def test_divergent_outcomes_flagged(self) -> None:
        runs = [
            make_run_entry(outcome="success"),
            make_run_entry(outcome="blocked"),
        ]
        write_run_history(self.repo_root, runs)
        result = DecisionDivergenceDetector().scan(self.repo_root)
        self.assertGreater(result["total_flagged"], 0)
        self.assertEqual(result["status"], "drift_detected")

    def test_high_severity_for_success_vs_blocked(self) -> None:
        runs = [
            make_run_entry(outcome="success"),
            make_run_entry(outcome="blocked"),
        ]
        write_run_history(self.repo_root, runs)
        result = DecisionDivergenceDetector().scan(self.repo_root)
        flagged = result["flagged_items"][0]
        self.assertEqual(flagged["severity"], "high")

    def test_runs_missing_fields_logged_not_crashed(self) -> None:
        runs = [
            make_run_entry(outcome="success"),
            make_run_entry(outcome="success", audience=None),
            make_run_entry(outcome="blocked", recipe_id=None),
        ]
        write_run_history(self.repo_root, runs)
        result = DecisionDivergenceDetector().scan(self.repo_root)
        self.assertGreaterEqual(result["current_value"]["skipped_runs"], 2)
        skipped_log = (
            self.repo_root / "governance" / "drift" / "skipped_runs.jsonl"
        )
        self.assertTrue(skipped_log.is_file())

    def test_single_run_groups_not_flagged(self) -> None:
        runs = [make_run_entry(outcome="success")]
        write_run_history(self.repo_root, runs)
        result = DecisionDivergenceDetector().scan(self.repo_root)
        self.assertEqual(result["total_flagged"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
