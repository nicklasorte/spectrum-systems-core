"""Tests for StoryExtractor (Phase C, Step 5 + Red Team #2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker, StoryExtractor

from ._fixtures import id_from_prompt, read_jsonl, write_text_units


def _ok_response(excerpt: str, prompt: str = "") -> str:
    payload = {
        "story_found": True,
        "source_excerpt": excerpt,
        "story_summary": "A short summary of the moment that mattered most.",
        "possible_theme": "agency comments",
        "tier_guess": "tier_1",
        "why_it_might_work": "Because there is a clear human moment at stake.",
        "risk_flags": [],
    }
    if prompt:
        payload["source_turn_ids"] = [id_from_prompt(prompt, "Chunk ID")]
    return json.dumps(payload)


def _no_story_response() -> str:
    return json.dumps(
        {
            "story_found": False,
            "source_excerpt": None,
            "story_summary": None,
            "possible_theme": None,
            "tier_guess": None,
            "why_it_might_work": None,
            "risk_flags": [],
        }
    )


class StoryExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-story-001"
        texts = [
            "Paragraph one introduces the central question.",
            "Paragraph two describes a difficult moment when the team realized risk.",
            "Paragraph three closes with an uncertain decision.",
            "Paragraph four adds context.",
            "Paragraph five sets up the agency comments segment.",
            "Paragraph six describes the agency findings.",
            "Paragraph seven describes the response.",
            "Paragraph eight introduces the next question.",
            "Paragraph nine answers the question with care.",
            "Paragraph ten ties the threads together.",
        ]
        write_text_units(
            self.repo_root, family="notes", source_id=self.source_id, texts=texts
        )
        Chunker().chunk(self.source_id, str(self.repo_root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load_chunks(self) -> list[dict]:
        path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "chunks.jsonl"
        )
        return read_jsonl(path)

    def _load_candidates(self) -> list[dict]:
        path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "candidates.jsonl"
        )
        return read_jsonl(path)

    def test_valid_extraction_produces_candidates(self) -> None:
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            return _ok_response(first_excerpt, prompt)

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertGreater(len(result["candidates"]), 0)
        for c in result["candidates"]:
            self.assertEqual(c["extraction_temperature"], 0)
            self.assertEqual(c["extraction_model"], "claude-haiku-4-5-20251001")
            self.assertEqual(c["status"], "candidate")
            self.assertEqual(c["source_id"], self.source_id)

    def test_api_error_produces_blocked_candidate_not_crash(self) -> None:
        def caller(prompt: str) -> str:
            raise RuntimeError("simulated API outage")

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        # Every chunk should produce a blocked record.
        records = self._load_candidates()
        self.assertEqual(len(records), len(self._load_chunks()))
        for record in records:
            self.assertEqual(record["status"], "blocked")
            self.assertIn("api_error", record["block_reason"])

    def test_no_story_found_produces_no_candidate(self) -> None:
        def caller(prompt: str) -> str:
            return _no_story_response()

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["candidates"]), 0)
        self.assertEqual(self._load_candidates(), [])

    def test_candidates_jsonl_overwritten_not_appended(self) -> None:
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            return _ok_response(first_excerpt, prompt)

        StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        first_run = self._load_candidates()
        StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        second_run = self._load_candidates()
        # Same number of records — appending would double the count.
        self.assertEqual(len(first_run), len(second_run))

    def test_temperature_zero_recorded_in_candidate(self) -> None:
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            return _ok_response(first_excerpt, prompt)

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertGreater(len(result["candidates"]), 0)
        for c in result["candidates"]:
            self.assertEqual(c["extraction_temperature"], 0)

    def test_invalid_json_response_blocked_not_crashed(self) -> None:
        def caller(prompt: str) -> str:
            return "not valid json"

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        records = self._load_candidates()
        for record in records:
            self.assertEqual(record["status"], "blocked")
            self.assertIn("json_parse_error", record["block_reason"])


if __name__ == "__main__":
    unittest.main()
