"""Tests for CommentProcessor + IssueRegistry + IssueEval (Phase D Steps 12-14 + RT4)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.paper import (
    CommentProcessor,
    IssueEval,
    IssueRegistry,
)

from ._fixtures import (
    make_issue,
    read_jsonl,
    write_source_record,
    write_text_units,
)


def _ok_position_response() -> str:
    return json.dumps(
        {
            "agency_name": "Bureau of Standards",
            "position_statement": (
                "The bureau objects to the proposed methodology because it lacks"
                " coverage of small-entity comments."
            ),
            "references_claim_text": None,
            "severity": "major",
        }
    )


class CommentProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "ns-paper-cmts-001"
        self.comment_id = "ns-cmts-001"
        # Comment source has structured comment text in its text units.
        write_text_units(
            self.repo_root,
            family="comments",
            source_id=self.comment_id,
            texts=[
                "The bureau objects to the proposed methodology and recommends a revised approach.",
                "Random unrelated chatter without any structured indicator words.",
            ],
        )
        # The paper source needs a processed dir for issues.jsonl.
        write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.paper_id,
            texts=["Working paper section text."],
        )
        write_source_record(
            self.repo_root, family="working_papers", source_id=self.paper_id
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _issues_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper" / "issues.jsonl"
        )

    def _warnings_path(self) -> Path:
        return (
            self.repo_root / "processed" / "working_papers" / self.paper_id
            / "paper" / "unstructured_warnings.jsonl"
        )

    def test_unstructured_comment_emits_warning(self) -> None:
        cp = CommentProcessor(api_caller=lambda _p: _ok_position_response())
        result = cp.process(
            "Random chatter without indicators.",
            self.paper_id,
            self.comment_id,
            str(self.repo_root),
        )
        self.assertEqual(result["status"], "warning_emitted")
        self.assertTrue(self._warnings_path().is_file())
        warnings = read_jsonl(self._warnings_path())
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0]["requires_human_tagging"])

    def test_structured_comment_creates_issue_record(self) -> None:
        cp = CommentProcessor(api_caller=lambda _p: _ok_position_response())
        result = cp.process(
            "The bureau recommends a revised methodology.",
            self.paper_id,
            self.comment_id,
            str(self.repo_root),
        )
        self.assertEqual(result["status"], "issue_created")
        artifact = result["artifact"]
        self.assertEqual(artifact["issue_type"], "agency_comment")
        self.assertIsNone(artifact["source_unit_id"])

    def test_warning_written_to_unstructured_warnings_jsonl(self) -> None:
        cp = CommentProcessor(api_caller=lambda _p: _ok_position_response())
        cp.process(
            "Random chatter without indicators.",
            self.paper_id,
            self.comment_id,
            str(self.repo_root),
        )
        self.assertTrue(self._warnings_path().is_file())

    def test_jaccard_duplicate_detection_in_registry(self) -> None:
        registry = IssueRegistry()
        a = make_issue(
            source_id=self.paper_id,
            description=(
                "Bureau objects proposed methodology small entity coverage missing comments."
            ),
            issue_type="agency_comment",
        )
        b = make_issue(
            source_id=self.paper_id,
            description=(
                "Bureau objects proposed methodology small entity coverage missing comments review."
            ),
            issue_type="agency_comment",
        )
        registry.add_issue(a, str(self.repo_root), self.paper_id)
        registry.add_issue(b, str(self.repo_root), self.paper_id)
        issues = read_jsonl(self._issues_path())
        self.assertEqual(len(issues), 2)
        # The second issue must list the first as a similar_issue_id.
        second = issues[1]
        self.assertIn(a["issue_id"], second["similar_issue_ids"])

    def test_non_comment_issue_without_unit_id_blocked(self) -> None:
        # An "unsupported_claim" issue must have a source_unit_id.
        bad = make_issue(
            source_id=self.paper_id,
            description=(
                "This unsupported claim issue has no unit reference at all here."
            ),
            issue_type="unsupported_claim",
            source_unit_id=None,
        )
        result = IssueEval().run([bad])
        self.assertEqual(result["decision"], "block")
        self.assertIn(
            "EVAL-ISSUE-004:source_traceability", result["reason_codes"]
        )

    def test_orphan_claim_id_blocked(self) -> None:
        bad = make_issue(
            source_id=self.paper_id,
            description=(
                "Issue references a claim that does not exist anywhere here."
            ),
            issue_type="agency_comment",
            claim_id="11111111-1111-1111-1111-111111111111",
        )
        result = IssueEval().run(
            [bad],
            working_paper_source_id=self.paper_id,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["decision"], "block")
        self.assertIn(
            "EVAL-ISSUE-003:orphan_claim_ids", result["reason_codes"]
        )

    def test_issues_accumulate_across_sources(self) -> None:
        # Two add_issue calls both append (RT4-005).
        registry = IssueRegistry()
        a = make_issue(
            source_id=self.paper_id,
            description=(
                "Issue from source A about the agency methodology and approach."
            ),
            issue_type="agency_comment",
        )
        b = make_issue(
            source_id=self.paper_id,
            description=(
                "Distinct concern about evidence completeness from a second source."
            ),
            issue_type="agency_comment",
        )
        registry.add_issue(a, str(self.repo_root), self.paper_id)
        registry.add_issue(b, str(self.repo_root), self.paper_id)
        issues = read_jsonl(self._issues_path())
        self.assertEqual(len(issues), 2)


if __name__ == "__main__":
    unittest.main()
