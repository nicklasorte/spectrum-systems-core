"""Tests for ContradictionDetector (Phase D, Step 9 + RT3)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import ContradictionDetector

from ._fixtures import make_claim, read_jsonl, write_text_units


class ContradictionDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-paper-contr-001"
        units = write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=["Unit zero text.", "Unit one text."],
        )
        self.units = units

        self.claim_pos = make_claim(
            source_id=self.source_id,
            source_unit_id=units[0]["unit_id"],
            claim_text=(
                "The agency methodology produces reliable comment outcomes "
                "across multiple proceedings."
            ),
            source_excerpt="Unit zero text",
            materiality="high",
        )
        self.claim_neg = make_claim(
            source_id=self.source_id,
            source_unit_id=units[1]["unit_id"],
            claim_text=(
                "The agency methodology does not produce reliable comment "
                "outcomes across multiple proceedings."
            ),
            source_excerpt="Unit one text",
            materiality="high",
        )

        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper"
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        with (paper_dir / "claims.jsonl").open("w", encoding="utf-8") as fh:
            for c in [self.claim_pos, self.claim_neg]:
                fh.write(json.dumps(c, sort_keys=True) + "\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "contradiction_summary.json"
        )

    def test_negation_pair_flagged(self) -> None:
        result = ContradictionDetector().run_on_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["contradiction_count"], 1)

    def test_unrelated_claims_not_flagged(self) -> None:
        unrelated = make_claim(
            source_id=self.source_id,
            source_unit_id=self.units[1]["unit_id"],
            claim_text=(
                "Snowfall accumulates rapidly along northern slopes during "
                "early winter periods."
            ),
            source_excerpt="Unit one text",
            materiality="medium",
        )
        # Replace claims.jsonl with unrelated pair.
        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper"
        )
        with (paper_dir / "claims.jsonl").open("w", encoding="utf-8") as fh:
            for c in [self.claim_pos, unrelated]:
                fh.write(json.dumps(c, sort_keys=True) + "\n")
        result = ContradictionDetector().run_on_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["contradiction_count"], 0)

    def test_summary_written_with_zero_contradictions(self) -> None:
        # Empty claims case.
        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper"
        )
        (paper_dir / "claims.jsonl").write_text("", encoding="utf-8")
        result = ContradictionDetector().run_on_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["contradiction_count"], 0)
        self.assertTrue(self._summary_path().is_file())
        summary = json.loads(self._summary_path().read_text())
        self.assertEqual(summary["contradiction_count"], 0)

    def test_contradicted_by_ids_updated_in_claims_jsonl(self) -> None:
        ContradictionDetector().run_on_source(
            self.source_id, str(self.repo_root)
        )
        claims_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "claims.jsonl"
        )
        claims = read_jsonl(claims_path)
        ids = [c["claim_id"] for c in claims]
        for c in claims:
            other = ids[1] if c["claim_id"] == ids[0] else ids[0]
            self.assertIn(other, c.get("contradicted_by_claim_ids", []))


if __name__ == "__main__":
    unittest.main()
