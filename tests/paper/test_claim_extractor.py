"""Tests for ClaimExtractor (Phase D, Step 4 + Red Team #2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict

from spectrum_systems_core.paper import ClaimEval, ClaimExtractor

from ._fixtures import id_from_prompt, read_jsonl, write_text_units


def _ok_response(excerpt: str, prompt: str = "") -> str:
    claim: Dict[str, object] = {
        "claim_text": "The agency requires comments by Friday.",
        "claim_type": "factual",
        "materiality": "high",
        "source_excerpt": excerpt,
    }
    if prompt:
        claim["source_turn_ids"] = [id_from_prompt(prompt, "Unit ID")]
    return json.dumps({"claims": [claim]})


def _empty_response() -> str:
    return json.dumps({"claims": []})


class ClaimExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-paper-001"
        self.texts = [
            "The agency requires comments by Friday and details a deadline for filings.",
            "Section two describes methodology used by reviewers in this proceeding.",
            "Short.",
        ]
        write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=self.texts,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "claims.jsonl"
        )

    def test_valid_extraction_produces_claims(self) -> None:
        excerpt = "The agency requires comments by Friday"
        ext = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        result = ext.extract_from_source(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["claims"]), 1)
        for claim in result["claims"]:
            self.assertEqual(claim["extraction_temperature"], 0)
            self.assertTrue(claim["source_unit_id"])

    def test_hallucinated_excerpt_blocked_by_eval(self) -> None:
        # Mock returns an excerpt not in any text unit.
        ext = ClaimExtractor(
            api_caller=lambda p: _ok_response("This excerpt is fabricated.", p)
        )
        result = ext.extract_from_source(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        # ClaimEval must block on grounding.
        eval_result = ClaimEval().run(
            result["claims"], [], self.source_id, str(self.repo_root)
        )
        self.assertEqual(eval_result["decision"], "block")
        self.assertIn(
            "EVAL-CLAIM-002:source_grounding", eval_result["reason_codes"]
        )

    def test_api_exception_skips_unit_not_crashes(self) -> None:
        calls = {"n": 0}

        def caller(_p: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _empty_response()

        ext = ClaimExtractor(api_caller=caller)
        result = ext.extract_from_source(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")

    def test_claims_jsonl_overwritten_not_appended(self) -> None:
        excerpt = "The agency requires comments by Friday"
        ext1 = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        first = ext1.extract_from_source(self.source_id, str(self.repo_root))
        first_count = len(read_jsonl(self._claims_path()))
        ext2 = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        ext2.extract_from_source(self.source_id, str(self.repo_root))
        second_count = len(read_jsonl(self._claims_path()))
        self.assertEqual(first_count, second_count)
        ids = [c["claim_id"] for c in read_jsonl(self._claims_path())]
        self.assertEqual(len(ids), len(set(ids)))

    def test_temperature_zero_recorded(self) -> None:
        excerpt = "The agency requires comments by Friday"
        ext = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        ext.extract_from_source(self.source_id, str(self.repo_root))
        for claim in read_jsonl(self._claims_path()):
            self.assertEqual(claim["extraction_temperature"], 0)
            self.assertEqual(
                claim["extraction_model"], "claude-haiku-4-5-20251001"
            )

    def test_unit_id_present_on_all_claims(self) -> None:
        excerpt = "The agency requires comments by Friday"
        ext = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        ext.extract_from_source(self.source_id, str(self.repo_root))
        for claim in read_jsonl(self._claims_path()):
            self.assertTrue(claim["source_unit_id"])

    def test_claims_md_written_as_view_only(self) -> None:
        excerpt = "The agency requires comments by Friday"
        ext = ClaimExtractor(api_caller=lambda p: _ok_response(excerpt, p))
        ext.extract_from_source(self.source_id, str(self.repo_root))
        md_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "markdown" / "claims.md"
        )
        self.assertTrue(md_path.is_file())
        body = md_path.read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", body)


if __name__ == "__main__":
    unittest.main()
