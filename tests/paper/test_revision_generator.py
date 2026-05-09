"""Tests for RevisionGenerator + RevisionEval (Phase D Steps 16-17 + RT findings)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import RevisionEval, RevisionGenerator

from ._fixtures import (
    make_claim,
    make_issue,
    make_revision_instruction,
    read_jsonl,
    write_text_units,
)


def _ok_response() -> str:
    return json.dumps(
        {
            "target_section": "Section II",
            "instruction_text": "Add a citation supporting the materiality claim.",
            "expected_outcome": "Claim properly supported.",
            "instruction_type": "add_evidence",
        }
    )


class RevisionGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "ns-paper-rev-001"
        units = write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.paper_id,
            texts=["Working paper section text."],
        )
        self.units = units
        self.claim = make_claim(
            source_id=self.paper_id,
            source_unit_id=units[0]["unit_id"],
            claim_text="The methodology is consistent across reviewers.",
            source_excerpt="Working paper section text",
            materiality="high",
        )
        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper"
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        with (paper_dir / "claims.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.claim, sort_keys=True) + "\n")
        # Create one open issue tied to the claim.
        self.issue = make_issue(
            source_id=self.paper_id,
            description=(
                "Reviewer raised concern about lack of explicit evidence "
                "supporting the methodology consistency claim."
            ),
            issue_type="missing_evidence",
            source_unit_id=units[0]["unit_id"],
            claim_id=self.claim["claim_id"],
            severity="major",
        )
        with (paper_dir / "issues.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.issue, sort_keys=True) + "\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _instructions_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper" / "revision_instructions.jsonl"
        )

    def test_valid_instruction_generated(self) -> None:
        gen = RevisionGenerator(api_caller=lambda _p: _ok_response())
        result = gen.generate_for_issue(
            self.issue, [self.claim], self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        instr = result["instruction"]
        self.assertEqual(instr["status"], "pending")
        self.assertEqual(instr["extraction_temperature"], 0)

    def test_orphan_claim_id_blocked(self) -> None:
        # Create an issue whose claim_id does not exist in the registry.
        bad_issue = make_issue(
            source_id=self.paper_id,
            description=(
                "Issue references a claim that does not exist in the registry."
            ),
            issue_type="agency_comment",
            claim_id="22222222-2222-2222-2222-222222222222",
        )
        gen = RevisionGenerator(api_caller=lambda _p: _ok_response())
        result = gen.generate_for_issue(
            bad_issue, [self.claim], self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("orphan_claim_id", result["reason"])

    def test_missing_required_fields_blocked(self) -> None:
        bad = make_revision_instruction(
            issue_id=self.issue["issue_id"],
            target_section="",  # empty
            instruction_text="",  # empty
            expected_outcome="",  # empty
        )
        result = RevisionEval().run([bad], [self.claim])
        self.assertEqual(result["decision"], "block")
        self.assertIn(
            "EVAL-REV-001:required_fields_present", result["reason_codes"]
        )

    def test_temperature_zero_recorded(self) -> None:
        gen = RevisionGenerator(api_caller=lambda _p: _ok_response())
        result = gen.generate_for_issue(
            self.issue, [self.claim], self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["instruction"]["extraction_temperature"], 0)
        self.assertEqual(
            result["instruction"]["extraction_model"],
            "claude-haiku-4-5-20251001",
        )

    def test_revision_instructions_jsonl_written(self) -> None:
        gen = RevisionGenerator(api_caller=lambda _p: _ok_response())
        result = gen.generate_for_source(
            self.paper_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        path = self._instructions_path()
        self.assertTrue(path.is_file())
        records = read_jsonl(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
