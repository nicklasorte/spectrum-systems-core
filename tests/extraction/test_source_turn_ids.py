"""Phase M.1 — Source turn pointers in extraction artifacts.

Every extracted story, claim, and assumption must cite the speaker-turn
chunk IDs (chunk_id / source_unit_id) from which it was extracted. Items
without source_turn_ids are omitted; items with invalid IDs are kept but
flagged for human review.

Research basis: Box AI arXiv 2510.19334; Gladia April 2026.
"""
from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from spectrum_systems_core.extraction import Chunker, StoryExtractor
from spectrum_systems_core.ingestion._paths import schema_path
from spectrum_systems_core.paper import AssumptionExtractor, ClaimExtractor
from spectrum_systems_core.paper._paths import paper_schema_path

from ._fixtures import id_from_prompt, read_jsonl, write_text_units


# ---------------------------------------------------------------------------
# Schema-level tests
# ---------------------------------------------------------------------------


def _load(schema_loader, name: str) -> Dict[str, Any]:
    return json.loads(schema_loader(name).read_text(encoding="utf-8"))


def _valid_story_skeleton(chunk_id: str) -> Dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "story_id": str(uuid.uuid4()),
        "source_id": "src",
        "source_family": "notes",
        "chunk_id": chunk_id,
        "source_turn_ids": [chunk_id],
        "source_turn_validation": "verified",
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [],
        "source_excerpt": "verbatim excerpt over ten chars",
        "story_summary": "A short summary of the moment that mattered.",
        "possible_theme": "decision",
        "tier_guess": "tier_1",
        "why_it_might_work": "It has stakes and a five-second moment.",
        "risk_flags": [],
        "storyworthy_score": {
            "five_second_moment": 0,
            "stakes": 0,
            "central_question": 0,
            "vulnerability": 0,
            "narrative_compression": 0,
            "total": 0,
        },
        "storyworthy_verdict": "reject",
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "grounded": False,
        "grounded_unit_ids": [],
        "status": "candidate",
        "superseded_by": None,
        "created_at": "2026-05-11T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "story_extractor", "version": "1.1.0"},
            "input_artifact_ids": [chunk_id],
            "execution_fingerprint_hash": "sha256:" + ("a" * 64),
        },
    }


def _valid_claim_skeleton(unit_id: str) -> Dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "claim_id": str(uuid.uuid4()),
        "source_id": "src",
        "source_unit_id": unit_id,
        "source_turn_ids": [unit_id],
        "source_turn_validation": "verified",
        "source_excerpt": "verbatim excerpt over ten chars",
        "claim_text": "The agency requires comments by Friday.",
        "claim_type": "factual",
        "materiality": "high",
        "supported_by_evidence_ids": [],
        "contradicted_by_claim_ids": [],
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "status": "candidate",
        "created_at": "2026-05-11T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "claim_extractor", "version": "1.1.0"},
            "input_artifact_ids": [unit_id],
            "execution_fingerprint_hash": "sha256:" + ("a" * 64),
        },
    }


def _valid_assumption_skeleton(unit_id: str) -> Dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "assumption_id": str(uuid.uuid4()),
        "source_id": "src",
        "source_unit_id": unit_id,
        "source_turn_ids": [unit_id],
        "source_turn_validation": "verified",
        "source_excerpt": "verbatim excerpt over ten chars",
        "assumption_text": "Reviewers have access to the full record.",
        "assumption_type": "scope",
        "risk_if_wrong": "high",
        "explicit": True,
        "status": "candidate",
        "created_at": "2026-05-11T00:00:00+00:00",
        "provenance": {
            "produced_by": {
                "component": "assumption_extractor",
                "version": "1.1.0",
            },
            "input_artifact_ids": [unit_id],
            "execution_fingerprint_hash": "sha256:" + ("a" * 64),
        },
    }


class StorySchemaSourceTurnIdsTests(unittest.TestCase):
    """Schema-level enforcement on story_candidate."""

    def setUp(self) -> None:
        self.schema = _load(schema_path, "story_candidate")
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_story_artifact_requires_source_turn_ids(self) -> None:
        artifact = _valid_story_skeleton(str(uuid.uuid4()))
        del artifact["source_turn_ids"]
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_story_artifact_source_turn_ids_min_items_one(self) -> None:
        artifact = _valid_story_skeleton(str(uuid.uuid4()))
        artifact["source_turn_ids"] = []
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_story_artifact_source_turn_validation_enum(self) -> None:
        artifact = _valid_story_skeleton(str(uuid.uuid4()))
        artifact["source_turn_validation"] = "bogus"
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)
        for ok in ("verified", "invalid", "missing"):
            artifact["source_turn_validation"] = ok
            self.validator.validate(artifact)

    def test_story_schema_version_bumped(self) -> None:
        self.assertEqual(
            self.schema["properties"]["schema_version"]["const"], "1.1.0"
        )

    def test_story_schema_additional_properties_still_false(self) -> None:
        self.assertIs(self.schema["additionalProperties"], False)


class ClaimSchemaSourceTurnIdsTests(unittest.TestCase):
    """Schema-level enforcement on technical_claim."""

    def setUp(self) -> None:
        self.schema = _load(paper_schema_path, "technical_claim")
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_claim_artifact_requires_source_turn_ids(self) -> None:
        artifact = _valid_claim_skeleton(str(uuid.uuid4()))
        del artifact["source_turn_ids"]
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_claim_artifact_source_turn_ids_min_items_one(self) -> None:
        artifact = _valid_claim_skeleton(str(uuid.uuid4()))
        artifact["source_turn_ids"] = []
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_claim_schema_version_bumped(self) -> None:
        self.assertEqual(
            self.schema["properties"]["schema_version"]["const"], "1.1.0"
        )

    def test_claim_schema_additional_properties_still_false(self) -> None:
        self.assertIs(self.schema["additionalProperties"], False)


class AssumptionSchemaSourceTurnIdsTests(unittest.TestCase):
    """Schema-level enforcement on assumption_record."""

    def setUp(self) -> None:
        self.schema = _load(paper_schema_path, "assumption_record")
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_assumption_artifact_requires_source_turn_ids(self) -> None:
        artifact = _valid_assumption_skeleton(str(uuid.uuid4()))
        del artifact["source_turn_ids"]
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_assumption_artifact_source_turn_ids_min_items_one(self) -> None:
        artifact = _valid_assumption_skeleton(str(uuid.uuid4()))
        artifact["source_turn_ids"] = []
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(artifact)

    def test_assumption_schema_version_bumped(self) -> None:
        self.assertEqual(
            self.schema["properties"]["schema_version"]["const"], "1.1.0"
        )


# ---------------------------------------------------------------------------
# Extractor behavior tests
# ---------------------------------------------------------------------------


def _ok_story_response(
    excerpt: str, source_turn_ids: List[str] | None
) -> str:
    payload: Dict[str, Any] = {
        "story_found": True,
        "source_excerpt": excerpt,
        "story_summary": "A short summary of the moment that mattered most.",
        "possible_theme": "agency comments",
        "tier_guess": "tier_1",
        "why_it_might_work": "Because there is a clear human moment at stake.",
        "risk_flags": [],
    }
    if source_turn_ids is not None:
        payload["source_turn_ids"] = source_turn_ids
    return json.dumps(payload)


class StoryExtractorSourceTurnIdsTests(unittest.TestCase):
    """End-to-end behavior on StoryExtractor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-stm1-001"
        texts = [
            "Paragraph one introduces the central question.",
            "Paragraph two describes the moment of risk realization.",
            "Paragraph three closes with an uncertain decision.",
            "Paragraph four adds context.",
            "Paragraph five sets up the agency comments segment.",
        ]
        write_text_units(
            self.repo_root, family="notes", source_id=self.source_id, texts=texts
        )
        Chunker().chunk(self.source_id, str(self.repo_root))
        self.chunks = read_jsonl(
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "chunks.jsonl"
        )
        self.excerpt = self.chunks[0]["text"].split("\n", 1)[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, response_fn) -> Dict[str, Any]:
        return StoryExtractor(api_caller=response_fn).extract_from_source(
            self.source_id, str(self.repo_root)
        )

    def test_extracted_item_without_source_turns_omitted(self) -> None:
        """Mock returns story_found=True but no source_turn_ids — omitted."""
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger(
            "spectrum_systems_core.extraction.story_extractor"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            result = self._run(
                lambda _p: _ok_story_response(self.excerpt, None)
            )
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["candidates"]), 0)
        self.assertIn("story_missing_required_fields", log_capture.getvalue())

    def test_extracted_item_with_invalid_turn_id_flagged(self) -> None:
        """Mock returns a chunk_id not in inputs — kept, flagged invalid."""
        bogus = str(uuid.uuid4())
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger(
            "spectrum_systems_core.extraction.story_extractor"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            result = self._run(
                lambda _p: _ok_story_response(self.excerpt, [bogus])
            )
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["candidates"]), 1)
        for c in result["candidates"]:
            self.assertEqual(c["source_turn_validation"], "invalid")
        self.assertIn("extraction_invalid_source_turns", log_capture.getvalue())

    def test_extracted_item_with_valid_turn_ids_verified(self) -> None:
        """Mock echoes chunk_id from prompt — kept, flagged verified."""
        def caller(prompt: str) -> str:
            chunk_id = id_from_prompt(prompt, "Chunk ID")
            return _ok_story_response(self.excerpt, [chunk_id])

        result = self._run(caller)
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["candidates"]), 1)
        for c in result["candidates"]:
            self.assertEqual(c["source_turn_validation"], "verified")
            self.assertEqual(c["source_turn_ids"], [c["chunk_id"]])
            self.assertEqual(c["schema_version"], "1.1.0")


class ClaimExtractorSourceTurnIdsTests(unittest.TestCase):
    """End-to-end behavior on ClaimExtractor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-clm1-001"
        self.texts = [
            "The agency requires comments by Friday and details a deadline.",
            "Section two describes methodology used by reviewers in this case.",
        ]
        write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=self.texts,
        )
        units_path = (
            self.repo_root / "processed" / "working_papers" / self.source_id
            / "text_units.jsonl"
        )
        self.unit_ids = [u["unit_id"] for u in read_jsonl(units_path)]
        self.excerpt = "The agency requires comments by Friday"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _response(
        self, source_turn_ids: List[str] | None
    ) -> str:
        claim: Dict[str, Any] = {
            "claim_text": "The agency requires comments by Friday.",
            "claim_type": "factual",
            "materiality": "high",
            "source_excerpt": self.excerpt,
        }
        if source_turn_ids is not None:
            claim["source_turn_ids"] = source_turn_ids
        return json.dumps({"claims": [claim]})

    def test_claim_without_source_turns_omitted(self) -> None:
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger(
            "spectrum_systems_core.paper.claim_extractor"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            ext = ClaimExtractor(api_caller=lambda _p: self._response(None))
            result = ext.extract_from_source(
                self.source_id, str(self.repo_root)
            )
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["claims"]), 0)
        self.assertIn("extraction_missing_source_turns", log_capture.getvalue())

    def test_claim_with_invalid_turn_id_flagged(self) -> None:
        bogus = str(uuid.uuid4())
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger(
            "spectrum_systems_core.paper.claim_extractor"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            ext = ClaimExtractor(api_caller=lambda _p: self._response([bogus]))
            result = ext.extract_from_source(
                self.source_id, str(self.repo_root)
            )
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["claims"]), 1)
        for c in result["claims"]:
            self.assertEqual(c["source_turn_validation"], "invalid")
        self.assertIn("extraction_invalid_source_turns", log_capture.getvalue())

    def test_claim_with_valid_turn_ids_verified(self) -> None:
        def caller(prompt: str) -> str:
            uid = id_from_prompt(prompt, "Unit ID")
            return self._response([uid])

        ext = ClaimExtractor(api_caller=caller)
        result = ext.extract_from_source(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["claims"]), 1)
        for c in result["claims"]:
            self.assertEqual(c["source_turn_validation"], "verified")
            self.assertEqual(c["source_turn_ids"], [c["source_unit_id"]])
            self.assertEqual(c["schema_version"], "1.1.0")


class AssumptionExtractorSourceTurnIdsTests(unittest.TestCase):
    """End-to-end behavior on AssumptionExtractor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-asm1-001"
        self.texts = [
            "Reviewers have access to the full record and rely on it.",
            "The methodology follows standard agency procedures and norms.",
        ]
        write_text_units(
            self.repo_root,
            family="working_papers",
            source_id=self.source_id,
            texts=self.texts,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _response(
        self, source_turn_ids: List[str] | None
    ) -> str:
        item: Dict[str, Any] = {
            "assumption_text": "Reviewers have access to the full record.",
            "assumption_type": "scope",
            "risk_if_wrong": "high",
            "explicit": True,
            "source_excerpt": "Reviewers have access to the full record",
        }
        if source_turn_ids is not None:
            item["source_turn_ids"] = source_turn_ids
        return json.dumps({"assumptions": [item]})

    def test_assumption_without_source_turns_omitted(self) -> None:
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger(
            "spectrum_systems_core.paper.assumption_extractor"
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            ext = AssumptionExtractor(
                api_caller=lambda _p: self._response(None)
            )
            result = ext.extract_from_source(
                self.source_id, str(self.repo_root)
            )
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["assumptions"]), 0)
        self.assertIn("extraction_missing_source_turns", log_capture.getvalue())

    def test_assumption_with_valid_turn_ids_verified(self) -> None:
        def caller(prompt: str) -> str:
            uid = id_from_prompt(prompt, "Unit ID")
            return self._response([uid])

        ext = AssumptionExtractor(api_caller=caller)
        result = ext.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(len(result["assumptions"]), 1)
        for a in result["assumptions"]:
            self.assertEqual(a["source_turn_validation"], "verified")
            self.assertEqual(a["source_turn_ids"], [a["source_unit_id"]])
            self.assertEqual(a["schema_version"], "1.1.0")


# ---------------------------------------------------------------------------
# Prompt-content tests
# ---------------------------------------------------------------------------


class PromptCitationInstructionTests(unittest.TestCase):
    """The mandatory citation block must be present in every extraction prompt."""

    def _assert_citation_block(self, prompt: str) -> None:
        self.assertIn("source_turn_ids", prompt)
        self.assertIn("DO NOT include", prompt)
        self.assertIn("SOURCE CITATION REQUIREMENT", prompt)

    def test_story_prompt_includes_citation_instruction(self) -> None:
        from spectrum_systems_core.extraction.story_extractor import (
            PROMPT_TEMPLATE,
        )
        self._assert_citation_block(PROMPT_TEMPLATE)

    def test_claim_prompt_includes_citation_instruction(self) -> None:
        from spectrum_systems_core.paper.claim_extractor import (
            CLAIM_EXTRACTION_PROMPT,
        )
        self._assert_citation_block(CLAIM_EXTRACTION_PROMPT)

    def test_assumption_prompt_includes_citation_instruction(self) -> None:
        from spectrum_systems_core.paper.assumption_extractor import (
            ASSUMPTION_EXTRACTION_PROMPT,
        )
        self._assert_citation_block(ASSUMPTION_EXTRACTION_PROMPT)


if __name__ == "__main__":
    unittest.main()
