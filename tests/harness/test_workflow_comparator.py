"""Tests for WorkflowComparator (Phase G — FINDING-G-005)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.harness import (
    RunHistoryStore,
    WorkflowComparator,
)

from ._fixtures import write_synthesis_run


def _record(repo_root: Path, run_id: str) -> None:
    manifest_path = repo_root / "synthesis" / run_id / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    RunHistoryStore().record_run(manifest, str(repo_root))


class WorkflowComparatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.cmp = WorkflowComparator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_compare_same_id_fails(self) -> None:
        run_id = write_synthesis_run(self.repo_root)
        _record(self.repo_root, run_id)
        result = self.cmp.compare(run_id, run_id, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertIn("same run", result["reason"])

    def test_compare_missing_run_fails(self) -> None:
        a = write_synthesis_run(self.repo_root)
        _record(self.repo_root, a)
        result = self.cmp.compare(
            a, "00000000-0000-4000-8000-deadbeefdead", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("not found", result["reason"])

    def test_compare_produces_all_dimensions(self) -> None:
        a = write_synthesis_run(self.repo_root, cost_usd=0.05, grounded_sections=2)
        b = write_synthesis_run(self.repo_root, cost_usd=0.03, grounded_sections=4)
        _record(self.repo_root, a)
        _record(self.repo_root, b)
        result = self.cmp.compare(a, b, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        json_path = Path(result["json_path"])
        self.assertTrue(json_path.is_file())
        comparison = json.loads(json_path.read_text())
        names = {d["dimension_name"] for d in comparison["dimensions"]}
        self.assertEqual(
            names,
            {
                "total_cost_usd",
                "eval_pass_count",
                "eval_fail_count",
                "eval_warn_count",
                "grounded_section_count",
                "ungrounded_section_count",
                "keynote_arc_beat_count",
                "run_duration_seconds",
            },
        )

    def test_direction_improved_for_lower_cost(self) -> None:
        a = write_synthesis_run(self.repo_root, cost_usd=0.05)
        b = write_synthesis_run(self.repo_root, cost_usd=0.03)
        _record(self.repo_root, a)
        _record(self.repo_root, b)
        result = self.cmp.compare(a, b, str(self.repo_root))
        comparison = json.loads(Path(result["json_path"]).read_text())
        cost = next(
            d for d in comparison["dimensions"]
            if d["dimension_name"] == "total_cost_usd"
        )
        self.assertEqual(cost["direction"], "improved")

    def test_direction_improved_for_more_grounded_sections(self) -> None:
        a = write_synthesis_run(
            self.repo_root, grounded_sections=1, ungrounded_sections=2
        )
        b = write_synthesis_run(
            self.repo_root, grounded_sections=4, ungrounded_sections=0
        )
        _record(self.repo_root, a)
        _record(self.repo_root, b)
        result = self.cmp.compare(a, b, str(self.repo_root))
        comparison = json.loads(Path(result["json_path"]).read_text())
        grounded = next(
            d for d in comparison["dimensions"]
            if d["dimension_name"] == "grounded_section_count"
        )
        self.assertEqual(grounded["direction"], "improved")

    def test_canonical_json_written(self) -> None:
        a = write_synthesis_run(self.repo_root)
        b = write_synthesis_run(self.repo_root)
        _record(self.repo_root, a)
        _record(self.repo_root, b)
        result = self.cmp.compare(a, b, str(self.repo_root))
        target = (
            self.repo_root
            / "harness" / "comparisons"
            / f"{a}_vs_{b}.json"
        )
        self.assertTrue(target.is_file())
        comparison = json.loads(target.read_text())
        self.assertEqual(comparison["run_id_a"], a)
        self.assertEqual(comparison["run_id_b"], b)

    def test_vault_projection_written_when_vault_provided(self) -> None:
        a = write_synthesis_run(self.repo_root)
        b = write_synthesis_run(self.repo_root)
        _record(self.repo_root, a)
        _record(self.repo_root, b)
        vault = self.repo_root / "vault"
        result = self.cmp.compare(a, b, str(self.repo_root), vault_root=str(vault))
        self.assertEqual(result["status"], "success")
        target = vault / "Harness" / "comparisons" / f"{a}_vs_{b}.md"
        self.assertTrue(target.is_file())
        body = target.read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", body)


if __name__ == "__main__":
    unittest.main()
