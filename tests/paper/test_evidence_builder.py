"""Tests for EvidenceBuilder + EvidenceEval (Phase D, Steps 8 + 10 + RT3)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import EvidenceBuilder, EvidenceEval

from ._fixtures import (
    make_claim,
    make_evidence,
    read_jsonl,
    write_source_record,
    write_text_units,
)


class EvidenceBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-paper-evid-001"
        self.texts = [
            "The agency requires comments by Friday and details deadline filings rules.",
            "Reviewers analyze comments quickly using consistent agency methodology.",
            "Methodology follows agency standards comments deadline reviewers consistent process.",
            "Unrelated section about a different topic entirely.",
        ]
        units = write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=self.texts,
        )
        self.units = units
        self.raw_hash = write_source_record(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
        )
        # Create a claim that maps to unit 0 with words also appearing in unit 2.
        claim = make_claim(
            source_id=self.source_id,
            source_unit_id=units[0]["unit_id"],
            claim_text=(
                "The agency requires comments by Friday and reviewers process them"
                " using consistent methodology."
            ),
            source_excerpt="The agency requires comments by Friday",
            materiality="high",
        )
        self.claim = claim
        # Persist a claims.jsonl so build_for_source works.
        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper"
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        with (paper_dir / "claims.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(claim, sort_keys=True) + "\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_evidence_built_for_high_materiality_claim(self) -> None:
        builder = EvidenceBuilder()
        result = builder.build_for_source(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        evidence_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "evidence.jsonl"
        )
        records = read_jsonl(evidence_path)
        self.assertGreaterEqual(len(records), 1)
        for r in records:
            self.assertEqual(r["claim_id"], self.claim["claim_id"])
            self.assertTrue(r["grounded"])
            self.assertEqual(r["source_record_hash"], self.raw_hash)

    def test_self_evidencing_blocked_by_eval(self) -> None:
        # Manually construct an evidence record that points at the same unit
        # as the claim — EVAL-EVID-004 must block.
        bad_evidence = make_evidence(
            claim_id=self.claim["claim_id"],
            source_id=self.source_id,
            source_unit_id=self.claim["source_unit_id"],
            source_excerpt="The agency requires comments by Friday",
            source_record_hash=self.raw_hash,
        )
        eval_result = EvidenceEval().run(
            [self.claim], [bad_evidence], self.source_id, str(self.repo_root)
        )
        self.assertEqual(eval_result["decision"], "block")
        self.assertIn(
            "EVAL-EVID-004:self_evidencing", eval_result["reason_codes"]
        )

    def test_stale_hash_produces_warn_not_block(self) -> None:
        # Build legitimate evidence first.
        EvidenceBuilder().build_for_source(self.source_id, str(self.repo_root))
        evidence_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "paper" / "evidence.jsonl"
        )
        evidence = read_jsonl(evidence_path)
        # Mutate the source_record raw_hash to simulate re-ingestion.
        sr_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "source_record.json"
        )
        sr = json.loads(sr_path.read_text())
        sr["payload"]["raw_hash"] = "sha256:" + ("c" * 64)
        sr_path.write_text(json.dumps(sr, indent=2, sort_keys=True) + "\n")

        eval_result = EvidenceEval().run(
            [self.claim], evidence, self.source_id, str(self.repo_root)
        )
        # decision must NOT be block due to stale hash alone.
        statuses = {e["name"]: e["status"] for e in eval_result["eval_results"]}
        self.assertEqual(statuses.get("EVAL-EVID-003"), "warn")
        # Decision blocks only on EVAL-EVID-002 (medium has no evidence) is irrelevant.
        # The point: stale-hash itself is not a blocker.
        self.assertNotIn(
            "EVAL-EVID-003:source_record_hash_current",
            eval_result["reason_codes"],
        )

    def test_no_evidence_for_medium_materiality_warns_not_blocks(self) -> None:
        med_claim = make_claim(
            source_id=self.source_id,
            source_unit_id=self.units[3]["unit_id"],
            claim_text=(
                "An entirely different concept aboutZZZ obscureXXXX terms unrelated."
            ),
            source_excerpt="Unrelated section about a different topic entirely",
            materiality="medium",
        )
        eval_result = EvidenceEval().run(
            [med_claim], [], self.source_id, str(self.repo_root)
        )
        # No high-materiality claim with no evidence => not blocked on -002.
        statuses = {e["name"]: e["status"] for e in eval_result["eval_results"]}
        self.assertEqual(statuses["EVAL-EVID-002"], "warn")
        self.assertEqual(eval_result["decision"], "allow")

    def test_no_evidence_for_high_materiality_blocks(self) -> None:
        # Use the fixture claim (high materiality) with no evidence.
        eval_result = EvidenceEval().run(
            [self.claim], [], self.source_id, str(self.repo_root)
        )
        self.assertEqual(eval_result["decision"], "block")
        self.assertIn(
            "EVAL-EVID-002:high_materiality_coverage",
            eval_result["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
