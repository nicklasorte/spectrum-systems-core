"""Tests for RevisionWorkflow (Phase D Step 18 + RT5 + FINDING-D-001)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import RevisionWorkflow

from ._fixtures import (
    make_claim,
    make_revision_instruction,
    read_jsonl,
    write_text_units,
)


class RevisionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "ns-paper-rw-001"
        # The "section" is the text of unit_0; we use unit_0's text as the
        # source section the revision targets.
        self.section_text = (
            "The agency requires comments by Friday and provides a methodology"
            " summary for reviewers."
        )
        units = write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.paper_id,
            texts=[self.section_text, "Section two text."],
        )
        self.units = units
        # High-materiality claim whose source_excerpt is a substring of the
        # original section.
        self.high_claim = make_claim(
            source_id=self.paper_id,
            source_unit_id=units[0]["unit_id"],
            claim_text="Friday is the deadline for comments.",
            source_excerpt="The agency requires comments by Friday",
            materiality="high",
        )
        paper_dir = (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper"
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        with (paper_dir / "claims.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.high_claim, sort_keys=True) + "\n")

        # Approved instruction targeting unit_0.
        self.instruction = make_revision_instruction(
            issue_id="33333333-3333-3333-3333-333333333333",
            target_section=units[0]["unit_id"],
            status="approved",
        )
        with (paper_dir / "revision_instructions.jsonl").open(
            "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(self.instruction, sort_keys=True) + "\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _diffs_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper" / "revision_diff.jsonl"
        )

    def _draft_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper" / "revised_draft.json"
        )

    def test_dropped_high_materiality_claim_blocks(self) -> None:
        # API returns text that drops the source_excerpt of the high-materiality claim.
        wf = RevisionWorkflow(
            api_caller=lambda _p: (
                "The methodology summary remains available for reviewers."
            )
        )
        result = wf.apply_instruction(
            self.instruction,
            self.section_text,
            [self.high_claim],
            self.paper_id,
            str(self.repo_root),
        )
        self.assertEqual(result["status"], "blocked")
        diff = result["revision_diff"]
        self.assertEqual(diff["status"], "blocked")
        self.assertIn(
            self.high_claim["claim_id"], diff["high_materiality_claims_dropped"]
        )

    def test_successful_revision_writes_diff(self) -> None:
        # API returns text that PRESERVES the high-materiality excerpt.
        wf = RevisionWorkflow(
            api_caller=lambda _p: (
                "The agency requires comments by Friday. Reviewers receive an"
                " expanded methodology summary."
            )
        )
        result = wf.apply_all_approved(
            self.paper_id,
            [self.instruction["instruction_id"]],
            str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["blocked"], 0)
        diffs = read_jsonl(self._diffs_path())
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["status"], "success")
        self.assertEqual(diffs[0]["revision_temperature"], 0)
        self.assertTrue(self._draft_path().is_file())

    def test_api_exception_returns_failure_not_crash(self) -> None:
        def caller(_p: str) -> str:
            raise RuntimeError("boom")

        wf = RevisionWorkflow(api_caller=caller)
        result = wf.apply_instruction(
            self.instruction,
            self.section_text,
            [self.high_claim],
            self.paper_id,
            str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        # No revised draft for failure path.
        self.assertFalse(self._draft_path().is_file())

    def test_approve_revisions_only_applies_approved(self) -> None:
        # Add a pending instruction and a separate approved one — only the
        # approved one is applied.
        pending = make_revision_instruction(
            issue_id="44444444-4444-4444-4444-444444444444",
            target_section=self.units[0]["unit_id"],
            status="pending",
        )
        paper_dir = self._diffs_path().parent
        with (paper_dir / "revision_instructions.jsonl").open(
            "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(self.instruction, sort_keys=True) + "\n")
            fh.write(json.dumps(pending, sort_keys=True) + "\n")

        wf = RevisionWorkflow(
            api_caller=lambda _p: (
                "The agency requires comments by Friday. Methodology unchanged."
            )
        )
        result = wf.apply_all_approved(
            self.paper_id,
            [self.instruction["instruction_id"], pending["instruction_id"]],
            str(self.repo_root),
        )
        # Only the approved instruction produces a diff.
        diffs = read_jsonl(self._diffs_path())
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["instruction_id"], self.instruction["instruction_id"])
        self.assertEqual(result["applied"], 1)

    def test_pending_instructions_not_auto_applied(self) -> None:
        # All pending — apply_all_approved must produce zero diffs.
        pending = make_revision_instruction(
            issue_id="55555555-5555-5555-5555-555555555555",
            target_section=self.units[0]["unit_id"],
            status="pending",
        )
        paper_dir = self._diffs_path().parent
        with (paper_dir / "revision_instructions.jsonl").open(
            "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(pending, sort_keys=True) + "\n")
        wf = RevisionWorkflow(api_caller=lambda _p: "should not be called")
        result = wf.apply_all_approved(
            self.paper_id, [pending["instruction_id"]], str(self.repo_root)
        )
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["blocked"], 0)
        self.assertFalse(self._diffs_path().is_file())

    def test_revision_diff_append_safe(self) -> None:
        # Apply twice in succession — the diff file accumulates entries.
        wf = RevisionWorkflow(
            api_caller=lambda _p: (
                "The agency requires comments by Friday. Round one revision."
            )
        )
        wf.apply_all_approved(
            self.paper_id,
            [self.instruction["instruction_id"]],
            str(self.repo_root),
        )
        # Approve a second instruction with the same target.
        second = make_revision_instruction(
            issue_id="66666666-6666-6666-6666-666666666666",
            target_section=self.units[0]["unit_id"],
            status="approved",
        )
        paper_dir = self._diffs_path().parent
        with (paper_dir / "revision_instructions.jsonl").open(
            "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(self.instruction, sort_keys=True) + "\n")
            fh.write(json.dumps(second, sort_keys=True) + "\n")

        wf2 = RevisionWorkflow(
            api_caller=lambda _p: (
                "The agency requires comments by Friday. Round two revision."
            )
        )
        wf2.apply_all_approved(
            self.paper_id, [second["instruction_id"]], str(self.repo_root)
        )

        diffs = read_jsonl(self._diffs_path())
        self.assertEqual(len(diffs), 2)


if __name__ == "__main__":
    unittest.main()
