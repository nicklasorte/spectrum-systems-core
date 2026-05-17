"""Tests for GroundingEval (FINDING-F-004, FINDING-F-007)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.cost_recorder import (
    MAX_SYNTHESIS_COST_USD,
    append_cost_record,
)
from spectrum_systems_core.synthesis.grounding_eval import GroundingEval


class _StubChecker:
    def __init__(self, known: set[str]) -> None:
        self._known = known

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._known


def _make_draft(
    run_id: str,
    *,
    citations_per_section: list,
    contents: list = None,
) -> dict:
    sections = []
    if contents is None:
        contents = ["Section content with citations here."] * len(citations_per_section)
    for i, (citations, content) in enumerate(zip(citations_per_section, contents)):
        sections.append(
            {
                "section_id": str(uuid.uuid4()),
                "section_title": f"Section {i}",
                "section_type": "background",
                "content": content,
                "inline_citations": list(citations),
                "grounded": False,
                "unverified_citations": [],
            }
        )
    bundle_hash = "sha256:" + ("a" * 64)
    return {
        "draft_id": str(uuid.uuid4()),
        "run_id": run_id,
        "bundle_id": str(uuid.uuid4()),
        "bundle_hash": bundle_hash,
        "audience": "technical",
        "title": "Test Report",
        "sections": sections,
        "generation_model": "claude-sonnet-4-20250514",
        "generation_temperature": 0,
        "status": "draft",
        "created_at": "2024-01-01T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("b" * 64),
        },
    }


class GroundingEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.run_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fabricated_artifact_id_blocks(self) -> None:
        fake_id = "00000000-0000-0000-0000-000000000000"
        draft = _make_draft(
            self.run_id, citations_per_section=[[fake_id]]
        )
        result = GroundingEval(checker=_StubChecker(set())).run(
            draft, str(self.repo_root)
        )
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any(rc.startswith("EVAL-GEN-002") for rc in result["reason_codes"])
        )

    def test_valid_citation_passes(self) -> None:
        good_id = str(uuid.uuid4())
        draft = _make_draft(self.run_id, citations_per_section=[[good_id]])
        result = GroundingEval(checker=_StubChecker({good_id})).run(
            draft, str(self.repo_root)
        )
        self.assertIn(result["decision"], {"allow", "warn"})
        self.assertEqual(result["reason_codes"], [])

    def test_no_citations_sets_grounded_false(self) -> None:
        long_content = "X" * 100  # length >= MIN_CONTENT_FOR_CITATION
        draft = _make_draft(
            self.run_id,
            citations_per_section=[[]],
            contents=[long_content],
        )
        result = GroundingEval(checker=_StubChecker(set())).run(
            draft, str(self.repo_root)
        )
        # No fabricated ids — only warn (not block) on missing citations.
        self.assertNotEqual(result["decision"], "block")
        self.assertTrue(
            any(wc.startswith("EVAL-GEN-002") for wc in result["warn_codes"])
        )
        # Reload draft from disk to verify grounded was rewritten as False.
        path = (
            self.repo_root / "synthesis" / self.run_id / "report_draft.json"
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(on_disk["sections"][0]["grounded"])

    def test_cost_over_threshold_warns_not_blocks(self) -> None:
        good_id = str(uuid.uuid4())
        draft = _make_draft(self.run_id, citations_per_section=[[good_id]])
        # Push cost above MAX_SYNTHESIS_COST_USD by appending a big record.
        append_cost_record(
            self.run_id,
            str(self.repo_root),
            call_purpose="forced_overrun",
            input_tokens=10_000_000,
            output_tokens=10_000_000,
            model="claude-sonnet-4-20250514",
        )
        result = GroundingEval(checker=_StubChecker({good_id})).run(
            draft, str(self.repo_root)
        )
        self.assertNotEqual(result["decision"], "block")
        self.assertTrue(
            any(wc.startswith("EVAL-GEN-004") for wc in result["warn_codes"])
        )
        self.assertGreater(result["total_cost_usd"], MAX_SYNTHESIS_COST_USD)

    def test_all_sections_grounded_sets_status(self) -> None:
        good_id = str(uuid.uuid4())
        draft = _make_draft(self.run_id, citations_per_section=[[good_id]])
        GroundingEval(checker=_StubChecker({good_id})).run(
            draft, str(self.repo_root)
        )
        path = (
            self.repo_root / "synthesis" / self.run_id / "report_draft.json"
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], "grounded")
        for section in on_disk["sections"]:
            self.assertTrue(section["grounded"])


if __name__ == "__main__":
    unittest.main()
