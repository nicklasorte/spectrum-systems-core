"""Tests for ChunkClassifier.

Phase M3.0. Verifies:
- a decision classification on approval text,
- the regulatory-verb fallback (off_topic + verb -> decision),
- off_topic with no verb stays off_topic,
- the produced artifact validates against the schema,
- the routing-quality warning fires at >20% off_topic (tested via merger).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from spectrum_systems_core.extraction.chunk_classifier import ChunkClassifier
from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "extraction"
    / "chunk_classification.schema.json"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _api_returns(classification: str, confidence=None):
    """Stub api_caller that returns a fixed classification."""
    def caller(_prompt: str) -> dict:
        return {"classification": classification, "confidence": confidence}
    return caller


class ChunkClassifierTests(unittest.TestCase):
    def test_decision_classification_on_approval_text(self) -> None:
        # Caller returns 'decision' directly.
        clf = ChunkClassifier(api_caller=_api_returns("decision", 0.92))
        chunk = {
            "chunk_id": "c1",
            "text": "LaSorte: We are applying ITU criteria. Any objections?",
        }
        result = clf.classify(chunk, source_id="mtg-001")
        self.assertEqual(result["classification"], "decision")
        self.assertFalse(result["regulatory_verb_fallback_applied"])
        self.assertEqual(result["confidence"], 0.92)

    def test_regulatory_verb_fallback_reclassifies_off_topic(self) -> None:
        # Caller returns 'off_topic' but chunk text contains 'approved'.
        clf = ChunkClassifier(api_caller=_api_returns("off_topic"))
        chunk = {"chunk_id": "c2", "text": "The motion was approved."}
        result = clf.classify(chunk, source_id="mtg-001")
        self.assertEqual(result["classification"], "decision")
        self.assertTrue(result["regulatory_verb_fallback_applied"])

    def test_regulatory_verb_fallback_is_case_insensitive(self) -> None:
        # Capital 'A' in 'Approved' must still trigger the fallback.
        clf = ChunkClassifier(api_caller=_api_returns("off_topic"))
        chunk = {"chunk_id": "c3", "text": "Approved. We will move on."}
        result = clf.classify(chunk, source_id="mtg-001")
        self.assertEqual(result["classification"], "decision")
        self.assertTrue(result["regulatory_verb_fallback_applied"])

    def test_off_topic_without_regulatory_verb_stays_off_topic(self) -> None:
        clf = ChunkClassifier(api_caller=_api_returns("off_topic"))
        chunk = {"chunk_id": "c4", "text": "Has anyone tried the new coffee?"}
        result = clf.classify(chunk, source_id="mtg-001")
        self.assertEqual(result["classification"], "off_topic")
        self.assertFalse(result["regulatory_verb_fallback_applied"])

    def test_word_boundary_prevents_false_positives(self) -> None:
        # 'preapproved' should NOT fire the regulatory-verb fallback.
        clf = ChunkClassifier(api_caller=_api_returns("off_topic"))
        chunk = {"chunk_id": "c5", "text": "This is a preapproved exception."}
        result = clf.classify(chunk, source_id="mtg-001")
        self.assertEqual(result["classification"], "off_topic")

    def test_api_error_classifies_as_off_topic(self) -> None:
        def boom(_prompt: str) -> dict:
            raise RuntimeError("LLM unavailable")

        clf = ChunkClassifier(api_caller=boom)
        result = clf.classify(
            {"chunk_id": "c6", "text": "Some text."}, source_id="mtg-001"
        )
        self.assertEqual(result["classification"], "off_topic")
        self.assertFalse(result["regulatory_verb_fallback_applied"])

    def test_invalid_classification_falls_back_to_off_topic(self) -> None:
        clf = ChunkClassifier(api_caller=_api_returns("nonsense"))
        result = clf.classify(
            {"chunk_id": "c7", "text": "x"}, source_id="mtg-001"
        )
        self.assertEqual(result["classification"], "off_topic")

    def test_chunk_classification_schema_validates(self) -> None:
        clf = ChunkClassifier(api_caller=_api_returns("decision", 0.5))
        result = clf.classify(
            {"chunk_id": "c8", "text": "Decision text."}, source_id="mtg-001"
        )
        schema = _load_schema()
        # Validates without raising.
        jsonschema.Draft202012Validator(schema).validate(result)

    def test_model_is_haiku(self) -> None:
        clf = ChunkClassifier()
        self.assertIn("haiku", ChunkClassifier.MODEL_ID.lower())
        # The instance's model defaults to the class MODEL_ID.
        result = clf.classify({"chunk_id": "c", "text": ""}, source_id="s")
        self.assertEqual(result["provenance"]["model"], ChunkClassifier.MODEL_ID)


class RoutingQualityWarningTests(unittest.TestCase):
    """The routing-quality warning lives in ExtractionMerger, but the
    test is grouped here per Phase M3 spec because it exercises the
    classification stream."""

    def _make_classifications(self, total: int, off_topic_count: int):
        """Return a list of fake classification dicts."""
        out = []
        for i in range(total):
            cls = "off_topic" if i < off_topic_count else "decision"
            out.append({
                "chunk_id": f"c{i}",
                "source_id": "mtg-001",
                "classification": cls,
                "regulatory_verb_fallback_applied": False,
                "confidence": None,
                "artifact_type": "chunk_classification",
                "schema_version": "1.0.0",
                "created_at": "1970-01-01T00:00:00+00:00",
                "provenance": {"produced_by": "ChunkClassifier", "model": "x"},
            })
        return out

    def test_routing_quality_warning_fires_at_threshold(self) -> None:
        # 21 of 100 chunks off-topic -> 21% > 20% -> warning True.
        classifications = self._make_classifications(100, off_topic_count=21)
        artifact = ExtractionMerger().merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=classifications,
            decisions=[], claims=[], action_items=[],
        )
        self.assertTrue(artifact["routing_quality_warning"])

    def test_routing_quality_warning_does_not_fire_at_20pct(self) -> None:
        # 20 of 100 chunks off-topic -> 20% NOT > 20% -> warning False.
        classifications = self._make_classifications(100, off_topic_count=20)
        artifact = ExtractionMerger().merge(
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            extraction_run_id="run-1",
            classifications=classifications,
            decisions=[], claims=[], action_items=[],
        )
        self.assertFalse(artifact["routing_quality_warning"])


if __name__ == "__main__":
    unittest.main()
