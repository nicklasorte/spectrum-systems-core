"""Tests for MitigationOutcomeTracker (FINDING-E-004)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.outcome_tracker import MitigationOutcomeTracker

from ._fixtures import (
    make_agency_comment_issue,
    make_mitigation,
    make_prediction,
    read_jsonl,
    write_paper_issue_records,
)


def _write_predictions_and_mitigations(
    repo_root: Path,
    family: str,
    source_id: str,
    predictions,
    mitigations,
) -> None:
    target = repo_root / "processed" / family / source_id / "paper" / "objections"
    target.mkdir(parents=True, exist_ok=True)
    with (target / "predictions.jsonl").open("w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(json.dumps(p, sort_keys=True) + "\n")
    with (target / "mitigations.jsonl").open("w", encoding="utf-8") as fh:
        for m in mitigations:
            fh.write(json.dumps(m, sort_keys=True) + "\n")


class MitigationOutcomeTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.paper_id = "paper-A"
        self.family = "working_papers"
        self.prediction = make_prediction(
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            evidence_basis=["pos-1"],
            confidence="medium",
            objection_type="methodology_concern",
        )
        self.prediction["no_evidence_basis_flag"] = False
        self.mitigation = make_mitigation(
            prediction_id=self.prediction["prediction_id"],
            agency_slug="fcc",
            mitigation_type="revise_claim",
        )
        _write_predictions_and_mitigations(
            self.repo_root,
            self.family,
            self.paper_id,
            [self.prediction],
            [self.mitigation],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_auto_downgrade_when_objection_recurs(self) -> None:
        # Build a secondary source with a critical agency_comment issue
        # (severity="critical" -> objection_type="technical_dispute").
        # We need it to match self.prediction's objection_type, so set
        # severity="major" -> "methodology_concern".
        secondary_id = "paper-B"
        recurring_issue = make_agency_comment_issue(
            paper_source_id=secondary_id,
            description="A long-form recurring concern about the same methodology choices used here.",
            severity="major",
        )
        write_paper_issue_records(
            self.repo_root,
            family=self.family,
            paper_source_id=secondary_id,
            issues=[recurring_issue],
        )
        # Create the agency dir so MitigationOutcomeTracker has a place to write.
        (self.repo_root / "agency" / "fcc").mkdir(parents=True, exist_ok=True)
        result = MitigationOutcomeTracker().record_outcome(
            mitigation_id=self.mitigation["mitigation_id"],
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            human_marked_outcome="effective",
            secondary_check_source_id=secondary_id,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["final_outcome"], "ineffective")
        self.assertTrue(result["auto_downgraded"])

    def test_human_mark_preserved_without_secondary_source(self) -> None:
        (self.repo_root / "agency" / "fcc").mkdir(parents=True, exist_ok=True)
        result = MitigationOutcomeTracker().record_outcome(
            mitigation_id=self.mitigation["mitigation_id"],
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            human_marked_outcome="effective",
            secondary_check_source_id=None,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["final_outcome"], "effective")
        self.assertFalse(result["auto_downgraded"])
        self.assertIsNone(result["outcome"]["secondary_check_objection_recurred"])

    def test_secondary_source_with_no_match_preserves_mark(self) -> None:
        # Secondary source exists but has no agency_comment issues at all.
        secondary_id = "paper-empty"
        write_paper_issue_records(
            self.repo_root,
            family=self.family,
            paper_source_id=secondary_id,
            issues=[],
        )
        (self.repo_root / "agency" / "fcc").mkdir(parents=True, exist_ok=True)
        result = MitigationOutcomeTracker().record_outcome(
            mitigation_id=self.mitigation["mitigation_id"],
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            human_marked_outcome="partial",
            secondary_check_source_id=secondary_id,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["final_outcome"], "partial")
        self.assertFalse(result["auto_downgraded"])

    def test_outcome_record_schema_valid(self) -> None:
        (self.repo_root / "agency" / "fcc").mkdir(parents=True, exist_ok=True)
        MitigationOutcomeTracker().record_outcome(
            mitigation_id=self.mitigation["mitigation_id"],
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            human_marked_outcome="effective",
            repo_root=str(self.repo_root),
        )
        records = read_jsonl(
            self.repo_root / "agency" / "fcc" / "mitigation_outcomes.jsonl"
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["mitigation_id"], self.mitigation["mitigation_id"])
        self.assertEqual(record["final_outcome"], "effective")

    def test_mitigation_not_found_returns_failure(self) -> None:
        result = MitigationOutcomeTracker().record_outcome(
            mitigation_id="00000000-0000-0000-0000-000000000000",
            agency_slug="fcc",
            paper_source_id=self.paper_id,
            human_marked_outcome="effective",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "mitigation_not_found")


if __name__ == "__main__":
    unittest.main()
