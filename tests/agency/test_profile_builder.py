"""Tests for ProfileBuilder."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.profile_builder import ProfileBuilder

from ._fixtures import (
    make_agency_comment_issue,
    read_jsonl,
    write_paper_issue_records,
)


def _ok_position_response() -> str:
    return json.dumps(
        {
            "topic": "spectrum allocation methodology",
            "position_statement": (
                "The FCC objects to the chosen allocation methodology and "
                "requests revision."
            ),
            "position_type": "opposes",
            "confidence_basis": "comment text directly states this objection",
        }
    )


def _no_position_response() -> str:
    return json.dumps({"no_position": True})


class ProfileBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "paper-A"
        self.family = "working_papers"
        self.issue1 = make_agency_comment_issue(
            paper_source_id=self.paper_id,
            description=(
                "The FCC contends that the proposed allocation methodology "
                "fails to address adjacent-channel interference adequately."
            ),
        )
        self.issue2 = make_agency_comment_issue(
            paper_source_id=self.paper_id,
            description=(
                "The FCC objects to the scope which excludes broadcast bands "
                "from the analysis."
            ),
        )
        write_paper_issue_records(
            self.repo_root,
            family=self.family,
            paper_source_id=self.paper_id,
            issues=[self.issue1, self.issue2],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_agency_comment_issues_create_positions(self) -> None:
        responses = iter([_ok_position_response(), _ok_position_response()])
        builder = ProfileBuilder(api_caller=lambda _p: next(responses))
        result = builder.ingest_issues_into_profile(
            self.paper_id, "FCC", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["positions_added"], 2)
        self.assertEqual(result["agency_slug"], "fcc")
        positions = read_jsonl(
            self.repo_root / "agency" / "fcc" / "positions.jsonl"
        )
        self.assertEqual(len(positions), 2)

    def test_api_failure_skips_issue_not_crashes(self) -> None:
        def _raises(_prompt: str) -> str:
            raise RuntimeError("simulated API failure")

        builder = ProfileBuilder(api_caller=_raises)
        result = builder.ingest_issues_into_profile(
            self.paper_id, "FCC", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        # Both calls failed → 0 positions but warnings counted.
        self.assertEqual(result["positions_added"], 0)
        self.assertGreaterEqual(result["warnings"], 2)
        # Objection history still recorded for both issues.
        history = read_jsonl(
            self.repo_root / "agency" / "fcc" / "objection_history.jsonl"
        )
        self.assertEqual(len(history), 2)

    def test_no_position_returned_goes_to_history_only(self) -> None:
        responses = iter([_no_position_response(), _no_position_response()])
        builder = ProfileBuilder(api_caller=lambda _p: next(responses))
        result = builder.ingest_issues_into_profile(
            self.paper_id, "FCC", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["positions_added"], 0)
        self.assertEqual(result["history_added"], 2)

    def test_duplicate_agency_name_normalized(self) -> None:
        responses = iter([_ok_position_response(), _ok_position_response()])
        builder = ProfileBuilder(api_caller=lambda _p: next(responses))
        builder.ingest_issues_into_profile(
            self.paper_id, "FCC", str(self.repo_root)
        )
        # Second ingest with the long form should reuse the same slug.
        write_paper_issue_records(
            self.repo_root,
            family=self.family,
            paper_source_id="paper-B",
            issues=[self.issue1],
        )
        responses2 = iter([_ok_position_response()])
        builder2 = ProfileBuilder(api_caller=lambda _p: next(responses2))
        builder2.ingest_issues_into_profile(
            "paper-B",
            "Federal Communications Commission",
            str(self.repo_root),
        )
        # Only one profile directory exists.
        agency_dirs = list((self.repo_root / "agency").iterdir())
        # markdown subdir is inside agency/fcc — there's only one slug dir at top.
        self.assertEqual(len(agency_dirs), 1)
        self.assertEqual(agency_dirs[0].name, "fcc")

    def test_projection_written_after_ingestion(self) -> None:
        responses = iter([_ok_position_response(), _ok_position_response()])
        builder = ProfileBuilder(api_caller=lambda _p: next(responses))
        builder.ingest_issues_into_profile(
            self.paper_id, "FCC", str(self.repo_root)
        )
        projection = (
            self.repo_root / "agency" / "fcc" / "markdown" / "profile.md"
        )
        self.assertTrue(projection.is_file())
        content = projection.read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", content)


if __name__ == "__main__":
    unittest.main()
