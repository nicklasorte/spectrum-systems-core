"""Tests for AssumptionExtractor (Phase D, Step 5)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import AssumptionExtractor, ClaimEval

from ._fixtures import read_jsonl, write_text_units


def _explicit_response(excerpt: str) -> str:
    return json.dumps(
        {
            "assumptions": [
                {
                    "assumption_text": "Reviewers have access to the full record.",
                    "assumption_type": "scope",
                    "risk_if_wrong": "high",
                    "explicit": True,
                    "source_excerpt": excerpt,
                }
            ]
        }
    )


def _implicit_null_response() -> str:
    return json.dumps(
        {
            "assumptions": [
                {
                    "assumption_text": "Stakeholders share a common evidentiary baseline.",
                    "assumption_type": "policy",
                    "risk_if_wrong": "medium",
                    "explicit": False,
                    "source_excerpt": None,
                }
            ]
        }
    )


def _implicit_with_excerpt_response(excerpt: str) -> str:
    return json.dumps(
        {
            "assumptions": [
                {
                    "assumption_text": "Stakeholders share a common evidentiary baseline.",
                    "assumption_type": "policy",
                    "risk_if_wrong": "medium",
                    "explicit": False,
                    "source_excerpt": excerpt,
                }
            ]
        }
    )


class AssumptionExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-paper-assume-001"
        self.texts = [
            "Reviewers have access to the full record and base their conclusions on it.",
            "The methodology follows standard agency procedures for comment review.",
        ]
        write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=self.texts,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "assumptions.jsonl"
        )

    def test_explicit_assumption_with_excerpt(self) -> None:
        excerpt = "Reviewers have access to the full record"
        ext = AssumptionExtractor(
            api_caller=lambda _p: _explicit_response(excerpt)
        )
        result = ext.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        records = read_jsonl(self._path())
        self.assertGreaterEqual(len(records), 1)
        self.assertTrue(all(r["explicit"] is True for r in records))
        self.assertTrue(all(r["source_excerpt"] for r in records))

    def test_implicit_assumption_with_null_excerpt(self) -> None:
        ext = AssumptionExtractor(
            api_caller=lambda _p: _implicit_null_response()
        )
        result = ext.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        records = read_jsonl(self._path())
        self.assertGreaterEqual(len(records), 1)
        for r in records:
            self.assertIs(r["explicit"], False)
            self.assertIsNone(r["source_excerpt"])

    def test_implicit_assumption_with_excerpt_blocked(self) -> None:
        excerpt = "The methodology follows standard"
        ext = AssumptionExtractor(
            api_caller=lambda _p: _implicit_with_excerpt_response(excerpt)
        )
        result = ext.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        eval_result = ClaimEval().run(
            [], result["assumptions"], self.source_id, str(self.repo_root)
        )
        self.assertEqual(eval_result["decision"], "block")
        self.assertIn(
            "EVAL-ASSUMP-002:implicit_excerpt", eval_result["reason_codes"]
        )

    def test_api_exception_skips_not_crashes(self) -> None:
        calls = {"n": 0}

        def caller(_p: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _explicit_response("Reviewers have access to the full record")

        ext = AssumptionExtractor(api_caller=caller)
        result = ext.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
