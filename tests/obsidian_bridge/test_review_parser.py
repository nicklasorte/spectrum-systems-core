"""Tests for ObsidianReviewParser."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid

from spectrum_systems_core.obsidian_bridge.review_parser import (
    ObsidianReviewParser,
)


def _form(
    *,
    decision: str,
    reviewer_id: str = "reviewer-1",
    findings_md: str = "",
    artifact_id: str = "11111111-1111-4111-8111-111111111111",
) -> str:
    return (
        "---\n"
        f'review_for_artifact_id: "{artifact_id}"\n'
        'review_for_artifact_type: "decision_brief"\n'
        'pipeline_run_id: "run-1"\n'
        f'reviewer_id: "{reviewer_id}"\n'
        f'decision: "{decision}"\n'
        'reviewed_at: "2026-05-09T12:00:00Z"\n'
        "review_status: submitted\n"
        "---\n\n"
        f"# Review\n\n## Findings\n\n{findings_md}\n\n## Reviewer Notes\n\n"
    )


def _finding(severity: str) -> str:
    return (
        "### Finding 1\n\n"
        f"- **severity**: {severity}\n"
        "- **section**: scope\n"
        "- **description**: example issue\n"
        "- **required_action**: rewrite the section\n"
    )


def _write_form(vault_root: str, content: str) -> str:
    pending = os.path.join(vault_root, "Reviews", "Pending")
    os.makedirs(pending, exist_ok=True)
    path = os.path.join(pending, f"{uuid.uuid4()}_review.md")
    with open(path, "wb") as fh:
        fh.write(content.encode("utf-8"))
    return path


class ReviewParserTests(unittest.TestCase):

    def test_approve_no_findings_succeeds(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(vault, _form(decision="approve"))
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["artifact"]["payload"]["decision"], "approve")
            self.assertEqual(result["artifact"]["payload"]["findings"], [])

    def test_revise_with_s2_finding_succeeds(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(
                vault,
                _form(decision="revise", findings_md=_finding("S2")),
            )
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(
                result["artifact"]["payload"]["findings"][0]["severity"], "S2"
            )

    def test_block_with_s4_finding_succeeds(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(
                vault,
                _form(decision="block", findings_md=_finding("S4")),
            )
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["artifact"]["payload"]["decision"], "block")

    def test_s3_finding_without_block_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(
                vault,
                _form(decision="approve", findings_md=_finding("S3")),
            )
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "failure")

    def test_revise_without_findings_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(vault, _form(decision="revise"))
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["reason"], "revise_requires_findings")

    def test_block_without_critical_finding_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(
                vault,
                _form(decision="block", findings_md=_finding("S1")),
            )
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "failure")
            self.assertEqual(
                result["reason"], "block_requires_critical_finding"
            )

    def test_blank_reviewer_id_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            path = _write_form(vault, _form(decision="approve", reviewer_id=""))
            result = ObsidianReviewParser().parse(path, vault)
            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["reason"], "blank_reviewer_id")


if __name__ == "__main__":
    unittest.main()
