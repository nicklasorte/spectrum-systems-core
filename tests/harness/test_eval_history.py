"""Tests for EvalScoreHistory (Phase G)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.harness import EvalScoreHistory


class EvalScoreHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_eval_appends_not_overwrites(self) -> None:
        history = EvalScoreHistory()
        history.record_eval_results(
            "run-1",
            [{"name": "section_grounding", "status": "pass"}],
            "report_draft",
            str(self.repo_root),
        )
        history.record_eval_results(
            "run-1",
            [{"name": "section_grounding", "status": "fail"}],
            "report_draft",
            str(self.repo_root),
        )
        path = (
            self.repo_root / "harness" / "evals" / "report_draft_history.jsonl"
        )
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_get_pass_rate_correct(self) -> None:
        history = EvalScoreHistory()
        for status in ("pass", "pass", "fail", "pass"):
            history.record_eval_results(
                "run-1",
                [{"name": "g", "status": status}],
                "report_draft",
                str(self.repo_root),
            )
        rate = history.get_pass_rate(
            "g", "report_draft", str(self.repo_root)
        )
        self.assertEqual(rate["total"], 4)
        self.assertEqual(rate["pass"], 3)
        self.assertEqual(rate["fail"], 1)
        self.assertAlmostEqual(rate["pass_rate"], 0.75)

    def test_get_pass_rate_returns_none_on_empty_history(self) -> None:
        history = EvalScoreHistory()
        rate = history.get_pass_rate(
            "missing", "missing_type", str(self.repo_root)
        )
        self.assertIsNone(rate["pass_rate"])
        self.assertEqual(rate["total"], 0)

    def test_get_degrading_evals_below_threshold(self) -> None:
        history = EvalScoreHistory()
        # 3 fails, 1 pass = 25% pass rate (below 0.8).
        for status in ("fail", "fail", "fail", "pass"):
            history.record_eval_results(
                "run-1",
                [{"name": "shaky", "status": status}],
                "report_draft",
                str(self.repo_root),
            )
        # All-pass eval (above threshold).
        for _ in range(5):
            history.record_eval_results(
                "run-2",
                [{"name": "stable", "status": "pass"}],
                "report_draft",
                str(self.repo_root),
            )
        degrading = history.get_degrading_evals(str(self.repo_root))
        names = {d["eval_name"] for d in degrading}
        self.assertIn("shaky", names)
        self.assertNotIn("stable", names)

    def test_degrading_evals_empty_on_empty_dir(self) -> None:
        result = EvalScoreHistory().get_degrading_evals(str(self.repo_root))
        self.assertEqual(result, [])

    def test_projection_highlights_degrading_evals(self) -> None:
        history = EvalScoreHistory()
        for status in ("fail", "fail", "fail", "pass"):
            history.record_eval_results(
                "run-1",
                [{"name": "shaky", "status": status}],
                "report_draft",
                str(self.repo_root),
            )
        path = history.write_eval_history_projection(str(self.repo_root))
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", body)
        self.assertIn("degrading", body)


if __name__ == "__main__":
    unittest.main()
