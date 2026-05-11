"""Tests for ExtractionMerger.

Phase M3.1. Verifies:
- overlapping source_turn_ids across extractors are flagged but kept,
- exact text duplicates inside the same extractor ARE removed,
- the merged artifact validates against the meeting_extraction schema,
- atomic write semantics.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "extraction"
    / "meeting_extraction.schema.json"
)


def _decision(text: str, turn_ids: list[str], dtype: str = "approved") -> dict:
    return {
        "decision_text": text,
        "decision_type": dtype,
        "stakeholders": [],
        "rationale": None,
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
    }


def _claim(text: str, turn_ids: list[str]) -> dict:
    return {
        "claim_text": text,
        "claim_type": "technical",
        "speaker": "X",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
    }


def _action(text: str, turn_ids: list[str]) -> dict:
    return {
        "action": text,
        "owner": "Alice",
        "due": None,
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
    }


class ExtractionMergerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.merger = ExtractionMerger()

    def test_overlapping_source_turns_flagged_not_removed(self) -> None:
        # A decision and a claim share chunk c10.
        decisions = [_decision("Approve plan A", ["c10"])]
        claims = [_claim("Plan A reduces I/N by 3 dB", ["c10"])]

        artifact = self.merger.merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=[],
            decisions=decisions,
            claims=claims,
            action_items=[],
        )
        # Both items must be present
        self.assertEqual(len(artifact["decisions"]), 1)
        self.assertEqual(len(artifact["claims"]), 1)
        # And the decision must be flagged
        self.assertTrue(artifact["decisions"][0]["requires_human_dedup"])
        self.assertEqual(artifact["requires_human_dedup_count"], 2)
        # Verify the claim is also flagged (cross-extractor overlap)
        self.assertTrue(artifact["claims"][0]["requires_human_dedup"])

    def test_exact_text_duplicates_within_extractor_removed(self) -> None:
        # Two identical decision_text strings from the same extractor.
        decisions = [
            _decision("Approve plan A", ["c1"]),
            _decision("Approve plan A", ["c2"]),
            _decision("Reject plan B", ["c3"]),
        ]
        artifact = self.merger.merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=[],
            decisions=decisions,
            claims=[],
            action_items=[],
        )
        texts = [d["decision_text"] for d in artifact["decisions"]]
        self.assertEqual(sorted(texts), ["Approve plan A", "Reject plan B"])

    def test_meeting_extraction_schema_validates(self) -> None:
        decisions = [_decision("X", ["t1"])]
        claims = [_claim("Y", ["t2"])]
        actions = [_action("Z", ["t3"])]
        artifact = self.merger.merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=[{
                "chunk_id": "t1", "source_id": "s", "classification": "decision",
                "regulatory_verb_fallback_applied": False, "confidence": None,
                "artifact_type": "chunk_classification",
                "schema_version": "1.0.0",
                "created_at": "1970-01-01T00:00:00+00:00",
                "provenance": {"produced_by": "ChunkClassifier", "model": "x"},
            }],
            decisions=decisions,
            claims=claims,
            action_items=actions,
        )
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(artifact)

    def test_write_to_is_atomic(self) -> None:
        artifact = self.merger.merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=[],
            decisions=[],
            claims=[],
            action_items=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deep" / "deeper" / "x.json"
            ExtractionMerger.write_to(artifact, out)
            self.assertTrue(out.is_file())
            # No leftover .tmp file
            tmp_path = out.with_suffix(out.suffix + ".tmp")
            self.assertFalse(tmp_path.exists())

    def test_no_cross_extractor_overlap_zero_count(self) -> None:
        # Different turn_ids -> no flag, no dedup count.
        decisions = [_decision("D", ["t1"])]
        claims = [_claim("C", ["t2"])]
        artifact = self.merger.merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=[],
            decisions=decisions,
            claims=claims,
            action_items=[],
        )
        self.assertEqual(artifact["requires_human_dedup_count"], 0)
        self.assertNotIn("requires_human_dedup", artifact["decisions"][0])


if __name__ == "__main__":
    unittest.main()
