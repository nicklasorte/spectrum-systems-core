"""Phase S.2: confidence field verification tests.

Covers:
  1. Extraction prompt includes CONFIDENCE_SCORING_BLOCK.
  2. Model response with confidence fields passes schema validation.
  3. Model response missing confidence on a decision fails schema validation.
  4. confidence_field_check fires the ``confidence_field_missing`` finding
     when a live artifact is missing confidence.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.extraction._prompt_blocks import (
    CONFIDENCE_SCORING_BLOCK,
)
from spectrum_systems_core.extraction.claim_extractor import ClaimExtractor
from spectrum_systems_core.extraction.decision_extractor import DecisionExtractor
from spectrum_systems_core.health.confidence_field_check import scan_extractions
from spectrum_systems_core.validation import (
    ArtifactValidationError,
    validate_artifact,
)


def _base_artifact() -> dict:
    return {
        "artifact_type": "meeting_extraction",
        "schema_version": "1.0.0",
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "created_at": "2026-01-01T00:00:00+00:00",
        "decisions": [],
        "claims": [],
        "action_items": [],
        "total_chunks_classified": 0,
        "off_topic_count": 0,
        "regulatory_verb_fallback_count": 0,
        "routing_quality_warning": False,
        "requires_human_dedup_count": 0,
        "extraction_run_id": "tex-0001",
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": True,
        "confidence_threshold": 0.5,
        "low_confidence_item_count": 0,
        "provenance": {"produced_by": "ExtractionMerger"},
    }


def _decision_with_confidence(value: float = 0.9) -> dict:
    return {
        "decision_text": "Adopt the OOBE mask.",
        "decision_type": "approved",
        "stakeholders": ["FCC"],
        "rationale": None,
        "source_turn_ids": ["chunk-1"],
        "source_turn_validation": "verified",
        "confidence": float(value),
    }


def _decision_without_confidence() -> dict:
    d = _decision_with_confidence()
    d.pop("confidence")
    return d


def _claim_with_confidence(value: float = 0.8) -> dict:
    return {
        "claim_text": "Adjacent channel interference exceeds the threshold.",
        "claim_type": "technical",
        "speaker": "Engineer A",
        "source_turn_ids": ["chunk-2"],
        "source_turn_validation": "verified",
        "confidence": float(value),
    }


class ConfidencePromptTests(unittest.TestCase):
    def test_decision_prompt_includes_confidence_block(self) -> None:
        extractor = DecisionExtractor(api_caller=lambda p: {"items": []})
        prompt = extractor._build_prompt(
            chunks=[{"chunk_id": "c1", "speaker": "S", "text": "Hello."}],
            glossary_block="",
            few_shot_block="",
        )
        self.assertIn(CONFIDENCE_SCORING_BLOCK, prompt)

    def test_claim_prompt_includes_confidence_block(self) -> None:
        extractor = ClaimExtractor(api_caller=lambda p: {"items": []})
        prompt = extractor._build_prompt(
            chunks=[{"chunk_id": "c1", "speaker": "S", "text": "Hello."}],
            glossary_block="",
            few_shot_block="",
        )
        self.assertIn(CONFIDENCE_SCORING_BLOCK, prompt)


class ConfidenceSchemaValidationTests(unittest.TestCase):
    def test_artifact_with_confidence_passes(self) -> None:
        artifact = _base_artifact()
        artifact["decisions"] = [_decision_with_confidence(0.9)]
        artifact["claims"] = [_claim_with_confidence(0.7)]
        validate_artifact(artifact, "meeting_extraction")

    def test_artifact_missing_confidence_fails(self) -> None:
        artifact = _base_artifact()
        artifact["decisions"] = [_decision_without_confidence()]
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(artifact, "meeting_extraction")


class ConfidenceFieldCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        (self.data_lake / "store" / "artifacts" / "extractions").mkdir(
            parents=True, exist_ok=True
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_artifact(self, artifact: dict) -> Path:
        target = (
            self.data_lake
            / "store"
            / "artifacts"
            / "extractions"
            / f"{artifact['source_artifact_id']}_meeting_extraction.json"
        )
        target.write_text(
            json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
        )
        return target

    def test_finding_emitted_when_confidence_missing(self) -> None:
        artifact = _base_artifact()
        artifact["decisions"] = [_decision_without_confidence()]
        self._write_artifact(artifact)
        findings = scan_extractions(self.data_lake)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_code, "confidence_field_missing")
        self.assertEqual(findings[0].severity, "warn")
        self.assertEqual(findings[0].context["item_kind"], "decisions")
        self.assertEqual(findings[0].context["item_index"], 0)

    def test_no_finding_when_all_items_have_confidence(self) -> None:
        artifact = _base_artifact()
        artifact["decisions"] = [_decision_with_confidence(0.9)]
        artifact["claims"] = [_claim_with_confidence(0.6)]
        self._write_artifact(artifact)
        findings = scan_extractions(self.data_lake)
        self.assertEqual(findings, [])

    def test_no_findings_when_extraction_dir_missing(self) -> None:
        # Empty data lake -- no extractions/ dir exists yet.
        with tempfile.TemporaryDirectory() as tmp:
            findings = scan_extractions(Path(tmp))
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
