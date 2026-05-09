"""Tests for OutcomeMemoryStore (Phase G — FINDING-G-004)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict

from spectrum_systems_core.harness import OutcomeMemoryStore

from ._fixtures import read_jsonl


def _make_revision_diff(status: str = "success") -> Dict[str, Any]:
    return {
        "diff_id": str(uuid.uuid4()),
        "instruction_id": "i-1",
        "paper_source_id": "paper-A",
        "status": status,
    }


def _make_instruction() -> Dict[str, Any]:
    return {
        "instruction_id": "i-1",
        "issue_type": "scope_clarity",
        "instruction_text": (
            "Tighten the scope statement of section three to remove ambiguity "
            "around territory boundaries"
        ),
        "priority": "high",
    }


def _make_outcome_record(
    *,
    final_outcome: str,
    human_marked: str,
    secondary_check: bool = False,
) -> Dict[str, Any]:
    return {
        "outcome_id": str(uuid.uuid4()),
        "mitigation_id": str(uuid.uuid4()),
        "agency_slug": "fcc",
        "paper_source_id": "paper-A",
        "human_marked_outcome": human_marked,
        "secondary_check_source_id": "src-2" if secondary_check else None,
        "secondary_check_objection_recurred": True if secondary_check else None,
        "final_outcome": final_outcome,
        "outcome_note": "",
        "recorded_at": "2026-05-09T00:00:00+00:00",
    }


class OutcomeMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.store = OutcomeMemoryStore()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _records(self) -> list[Dict[str, Any]]:
        return read_jsonl(
            self.repo_root / "harness" / "outcomes" / "memory.jsonl"
        )

    def test_record_revision_outcome_type_correct(self) -> None:
        diff = _make_revision_diff(status="success")
        result = self.store.record_revision_outcome(
            diff, _make_instruction(), str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome_type"], "revision")
        self.assertEqual(records[0]["final_outcome"], "effective")
        self.assertFalse(records[0]["auto_downgraded"])

    def test_record_mitigation_outcome_auto_downgraded(self) -> None:
        outcome = _make_outcome_record(
            final_outcome="ineffective",
            human_marked="effective",
            secondary_check=True,
        )
        result = self.store.record_mitigation_outcome(outcome, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        record = self._records()[0]
        self.assertEqual(record["outcome_type"], "mitigation")
        self.assertTrue(record["auto_downgraded"])
        self.assertTrue(record["secondary_check_performed"])

    def test_find_similar_outcomes_empty_store(self) -> None:
        result = self.store.find_similar_outcomes(
            "any text", str(self.repo_root)
        )
        self.assertEqual(result, [])

    def test_find_similar_outcomes_returns_by_jaccard(self) -> None:
        diff = _make_revision_diff(status="success")
        self.store.record_revision_outcome(
            diff, _make_instruction(), str(self.repo_root)
        )
        result = self.store.find_similar_outcomes(
            "Tighten scope statement around territory boundaries section three",
            str(self.repo_root),
        )
        self.assertGreaterEqual(len(result), 1)
        record, score = result[0]
        self.assertGreater(score, 0.0)

    def test_effectiveness_rate_correct(self) -> None:
        for status in ("success", "success", "failure"):
            self.store.record_revision_outcome(
                _make_revision_diff(status=status),
                _make_instruction(),
                str(self.repo_root),
            )
        rate = self.store.get_effectiveness_rate("revision", str(self.repo_root))
        self.assertEqual(rate["total"], 3)
        self.assertEqual(rate["effective"], 2)
        self.assertAlmostEqual(rate["effectiveness_rate"], 2 / 3)

    def test_effectiveness_rate_none_on_empty(self) -> None:
        rate = self.store.get_effectiveness_rate("revision", str(self.repo_root))
        self.assertIsNone(rate["effectiveness_rate"])
        self.assertEqual(rate["total"], 0)


if __name__ == "__main__":
    unittest.main()
