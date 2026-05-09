"""Tests for GovernanceDashboard — Phase I."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import (
    DASHBOARD_SUMMARY_MAX_LINES,
    GovernanceDashboard,
)

from ._fixtures import (
    iso_days_ago,
    make_run_entry,
    stage_full_repo_copy,
    stage_minimal_repo,
    write_run_history,
)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_full_repo_copy(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dashboard_md_line_count_under_30(self) -> None:
        result = GovernanceDashboard().generate(self.repo_root)
        self.assertEqual(result["status"], "success")
        dashboard_md = (
            self.repo_root / "governance" / "markdown" / "dashboard.md"
        )
        self.assertTrue(dashboard_md.is_file())
        line_count = len(
            dashboard_md.read_text(encoding="utf-8").rstrip("\n").split("\n")
        )
        self.assertLessEqual(line_count, DASHBOARD_SUMMARY_MAX_LINES)

    def test_dashboard_includes_health_summary_sections(self) -> None:
        GovernanceDashboard().generate(self.repo_root)
        text = (
            self.repo_root / "governance" / "markdown" / "dashboard.md"
        ).read_text(encoding="utf-8")
        for marker in (
            "Schema health:",
            "Eval health:",
            "Decision consistency:",
            "Cost trend",
            "Hidden logic creep:",
        ):
            self.assertIn(marker, text)

    def test_top_5_drift_signals_max(self) -> None:
        # Force many divergent groups -> many drift_signals.
        runs = []
        for i in range(10):
            for outcome in ("success", "blocked"):
                runs.append(
                    make_run_entry(
                        outcome=outcome,
                        recipe_id=f"recipe_{i}",
                    )
                )
        write_run_history(self.repo_root, runs)
        result = GovernanceDashboard().generate(self.repo_root)
        dashboard_path = (
            self.repo_root / "governance" / "dashboard" / "latest.json"
        )
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        self.assertLessEqual(len(dashboard["drift_signals"]), 5)

    def test_top_5_candidates_max(self) -> None:
        # Add many lonely classes to trigger many candidates.
        for i in range(8):
            target = (
                self.repo_root
                / "src"
                / "spectrum_systems_core"
                / f"_lonely_{i}.py"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"class LonelyClass_{i}:\n    pass\n", encoding="utf-8"
            )
        GovernanceDashboard().generate(self.repo_root)
        dashboard = json.loads(
            (
                self.repo_root / "governance" / "dashboard" / "latest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertLessEqual(len(dashboard["top_candidates"]), 5)

    def test_view_only_banner_present(self) -> None:
        GovernanceDashboard().generate(self.repo_root)
        text = (
            self.repo_root / "governance" / "markdown" / "dashboard.md"
        ).read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
