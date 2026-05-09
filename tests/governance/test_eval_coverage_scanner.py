"""Tests for EvalCoverageScanner — Phase I."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.governance import EvalCoverageScanner

from ._fixtures import stage_minimal_repo, write_eval_history, write_py_file


def _add_eval_case(
    repo_root: Path,
    *,
    artifact_type: str,
    metric_name: str,
) -> None:
    eval_dir = repo_root / "contracts" / "evals"
    eval_dir.mkdir(parents=True, exist_ok=True)
    target = eval_dir / f"{artifact_type}_test_evals.json"
    target.write_text(
        json.dumps(
            [
                {
                    "id": "11111111-2222-4333-8444-555555555555",
                    "name": "EVAL-X-001",
                    "eval_type": "schema_conformance",
                    "metric_name": metric_name,
                    "target_artifact_type": artifact_type,
                    "required": True,
                    "pass_condition": "boolean",
                    "runner": "deterministic",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


class EvalCoverageScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        stage_minimal_repo(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_history_no_pass_rate_flags(self) -> None:
        result = EvalCoverageScanner().scan(self.repo_root)
        # Expect no never_failing/never_passing flags when there's no history.
        for f in result["flagged_items"]:
            self.assertNotIn(
                f["item_type"],
                {"never_failing_eval", "never_passing_eval", "degrading_eval"},
            )

    def test_uncovered_artifact_type_flagged(self) -> None:
        target = (
            self.repo_root
            / "contracts"
            / "schemas"
            / "fake_uncovered_artifact.schema.json"
        )
        target.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "x",
                    "title": "fake_uncovered_artifact",
                    "type": "object",
                }
            ),
            encoding="utf-8",
        )
        write_py_file(
            self.repo_root,
            "src/spectrum_systems_core/_test_module.py",
            "FAKE = 'fake_uncovered_artifact.schema.json'\n",
        )
        result = EvalCoverageScanner().scan(self.repo_root)
        uncovered = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "uncovered_artifact_type"
        ]
        self.assertTrue(uncovered)
        self.assertEqual(uncovered[0]["severity"], "high")

    def test_never_failing_eval_flagged_low(self) -> None:
        _add_eval_case(
            self.repo_root,
            artifact_type="custom_test_a",
            metric_name="custom_a.always_passes",
        )
        write_eval_history(
            self.repo_root,
            "custom_test_a",
            "custom_a.always_passes",
            ["pass"] * 25,
        )
        result = EvalCoverageScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "never_failing_eval"
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "low")

    def test_never_passing_eval_flagged_high(self) -> None:
        _add_eval_case(
            self.repo_root,
            artifact_type="custom_test_b",
            metric_name="custom_b.always_fails",
        )
        write_eval_history(
            self.repo_root,
            "custom_test_b",
            "custom_b.always_fails",
            ["fail"] * 6,
        )
        result = EvalCoverageScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "never_passing_eval"
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "high")

    def test_degrading_eval_flagged_medium(self) -> None:
        _add_eval_case(
            self.repo_root,
            artifact_type="custom_test_c",
            metric_name="custom_c.degrading",
        )
        # 7 fails out of 12 -> 0.42 pass rate, well below 0.8
        write_eval_history(
            self.repo_root,
            "custom_test_c",
            "custom_c.degrading",
            ["pass"] * 5 + ["fail"] * 7,
        )
        result = EvalCoverageScanner().scan(self.repo_root)
        flags = [
            f
            for f in result["flagged_items"]
            if f["item_type"] == "degrading_eval"
        ]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["severity"], "medium")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
