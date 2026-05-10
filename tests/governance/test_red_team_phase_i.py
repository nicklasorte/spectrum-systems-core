"""RT5 — full Phase I red team checks.

CHECK-RT5-001: NO AUTONOMOUS MUTATION (scanners never call os.remove etc.).
CHECK-RT5-002: PIPELINE INDEPENDENCE (governance failure does not break synthesis).
CHECK-RT5-003: dashboard.md line cap.
CHECK-RT5-005: HiddenLogicScanner is clean on Phase I code itself.
CHECK-RT5-006: MarkdownAuthorityScanner is clean on Phase I code itself.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import (
    DASHBOARD_SUMMARY_MAX_LINES,
    GovernanceDashboard,
    HiddenLogicScanner,
    MarkdownAuthorityScanner,
)

from ._fixtures import stage_full_repo_copy


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV_PKG = _REPO_ROOT / "src" / "spectrum_systems_core" / "governance"


_FORBIDDEN_MUTATION_CALLS = re.compile(
    r"(?<!\.)\b(os\.remove|os\.unlink|shutil\.rmtree|Path\.unlink)\b"
)


class RedTeamPhaseITests(unittest.TestCase):
    """Whole-Phase-I checks. Read source files in place where safe."""

    def test_rt5_001_no_autonomous_mutation_in_scanners(self) -> None:
        # Apply-compression is the ONLY allowed mutation site, and even there
        # remove/merge are recommendation-only.
        scanner_files = [
            _GOV_PKG / "schema_drift_scanner.py",
            _GOV_PKG / "eval_coverage_scanner.py",
            _GOV_PKG / "decision_divergence_detector.py",
            _GOV_PKG / "exception_accumulation_tracker.py",
            _GOV_PKG / "hidden_logic_scanner.py",
            _GOV_PKG / "markdown_authority_scanner.py",
            _GOV_PKG / "cost_trend_reporter.py",
            _GOV_PKG / "compression_scanner.py",
            _GOV_PKG / "dashboard.py",
        ]
        for path in scanner_files:
            text = path.read_text(encoding="utf-8")
            self.assertFalse(
                _FORBIDDEN_MUTATION_CALLS.search(text),
                f"forbidden mutation call in {path.name}",
            )

    def test_rt5_002_synthesis_pipeline_independence(self) -> None:
        """Governance scanner failure must not raise to caller of audit-governance."""
        import io
        import os

        from spectrum_systems_core.cli import audit_governance

        class BoomDashboard:
            def generate(self, *_a, **_kw):
                raise RuntimeError("boom")

        # Patch GovernanceDashboard import in cli.
        from spectrum_systems_core import cli as cli_module

        original = cli_module.GovernanceDashboard
        cli_module.GovernanceDashboard = BoomDashboard
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DATA_LAKE_PATH"] = tmp
                try:
                    buf = io.StringIO()
                    code = audit_governance(
                        vault=None,
                        repo_root=Path(tmp),
                        out_stream=buf,
                    )
                    # Failure is degraded into a warning + exit 0 — never blocks.
                    self.assertEqual(code, 0)
                    self.assertIn("warning", buf.getvalue().lower())
                finally:
                    os.environ.pop("DATA_LAKE_PATH", None)
        finally:
            cli_module.GovernanceDashboard = original

    def test_rt5_003_dashboard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            full_root = Path(tmp)
            stage_full_repo_copy(full_root)
            GovernanceDashboard().generate(full_root)
            md = (full_root / "governance" / "markdown" / "dashboard.md").read_text(
                encoding="utf-8"
            )
            line_count = len(md.rstrip("\n").split("\n"))
            self.assertLessEqual(line_count, DASHBOARD_SUMMARY_MAX_LINES)

    def test_rt5_005_hidden_logic_scanner_clean_on_phase_i_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            full_root = Path(tmp)
            stage_full_repo_copy(full_root)
            result = HiddenLogicScanner().scan(full_root)
            governance_high = [
                f
                for f in result["flagged_items"]
                if f["severity"] == "high"
                and "governance/" in f["item_id"]
            ]
            self.assertEqual(
                governance_high,
                [],
                f"governance code triggered hidden-logic flags: {governance_high}",
            )

    def test_rt5_006_markdown_authority_clean_on_phase_i_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            full_root = Path(tmp)
            stage_full_repo_copy(full_root)
            result = MarkdownAuthorityScanner().scan(full_root)
            governance_flags = [
                f
                for f in result["flagged_items"]
                if "governance/" in f["item_id"]
            ]
            self.assertEqual(governance_flags, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
