"""Tests for ReportGenerator (mocked Sonnet, FINDING-F-005, FINDING-F-007)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

from spectrum_systems_core.synthesis.report_generator import (
    ReportGenerator,
    SECTION_TYPES,
)

from ._fixtures import make_bundle, make_bundle_item


def _scripted_caller(answers: List[tuple]):
    """Return an api_caller that returns one answer per call."""
    iter_answers = iter(answers)

    def _call(_prompt: str):
        return next(iter_answers)

    return _call


class ReportGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.run_id = str(uuid.uuid4())
        self.items = [
            make_bundle_item(artifact_id=str(uuid.uuid4())),
            make_bundle_item(artifact_id=str(uuid.uuid4())),
            make_bundle_item(artifact_id=str(uuid.uuid4())),
        ]
        self.bundle = make_bundle(
            run_id=self.run_id, items=self.items
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scripted_with_citations(self) -> List[tuple]:
        cited = self.items[0]["artifact_id"]
        return [
            (
                f"This section content references [source: {cited}].",
                100,
                50,
            )
            for _ in SECTION_TYPES
        ]

    def test_all_sections_generated(self) -> None:
        gen = ReportGenerator(api_caller=_scripted_caller(
            self._scripted_with_citations()
        ))
        result = gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        draft_path = (
            self.repo_root / "synthesis" / self.run_id / "report_draft.json"
        )
        self.assertTrue(draft_path.is_file())
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(len(draft["sections"]), len(SECTION_TYPES))

    def test_inline_citations_extracted(self) -> None:
        gen = ReportGenerator(api_caller=_scripted_caller(
            self._scripted_with_citations()
        ))
        gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        draft = json.loads(
            (
                self.repo_root / "synthesis" / self.run_id / "report_draft.json"
            ).read_text(encoding="utf-8")
        )
        cited = self.items[0]["artifact_id"]
        for section in draft["sections"]:
            self.assertIn(cited, section["inline_citations"])

    def test_api_exception_produces_empty_section_not_crash(self) -> None:
        cited = self.items[0]["artifact_id"]
        good = (f"OK [source: {cited}].", 50, 25)
        answers = [good for _ in SECTION_TYPES]

        # Wrap the caller so that one call raises.
        boom_index = 2
        original_iter = iter(answers)

        def _call(_prompt: str):
            nonlocal boom_index
            boom_index -= 1
            if boom_index == 0:
                raise RuntimeError("simulated_api_error")
            return next(original_iter)

        gen = ReportGenerator(api_caller=_call)
        result = gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        draft = json.loads(
            (
                self.repo_root / "synthesis" / self.run_id / "report_draft.json"
            ).read_text(encoding="utf-8")
        )
        empty_sections = [s for s in draft["sections"] if s["content"] == ""]
        self.assertEqual(len(empty_sections), 1)
        for s in empty_sections:
            self.assertFalse(s["grounded"])

    def test_cost_record_written_per_section(self) -> None:
        gen = ReportGenerator(api_caller=_scripted_caller(
            self._scripted_with_citations()
        ))
        gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        cost_path = self.repo_root / "synthesis" / self.run_id / "cost.jsonl"
        self.assertTrue(cost_path.is_file())
        rows = [
            json.loads(line)
            for line in cost_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), len(SECTION_TYPES))
        for row in rows:
            self.assertEqual(row["model"], "claude-sonnet-4-20250514")

    def test_bundle_hash_in_report_matches_bundle(self) -> None:
        gen = ReportGenerator(api_caller=_scripted_caller(
            self._scripted_with_citations()
        ))
        gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        draft = json.loads(
            (
                self.repo_root / "synthesis" / self.run_id / "report_draft.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(draft["bundle_hash"], self.bundle["bundle_hash"])
        self.assertEqual(draft["bundle_id"], self.bundle["bundle_id"])

    def test_report_md_projection_written(self) -> None:
        gen = ReportGenerator(api_caller=_scripted_caller(
            self._scripted_with_citations()
        ))
        gen.generate(
            self.run_id, self.bundle, "technical", str(self.repo_root)
        )
        md_path = (
            self.repo_root / "synthesis" / self.run_id / "markdown" / "report.md"
        )
        self.assertTrue(md_path.is_file())
        self.assertIn("VIEW ONLY", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
