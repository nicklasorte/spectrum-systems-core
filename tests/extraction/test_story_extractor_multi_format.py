"""Tests for the multi-format story response parser.

Covers the three response formats the model may return:

- Format A — JSON array (correct):       ``[{...}, {...}]``
- Format B — single JSON object:         ``{...}``
- Format C — concatenated JSON objects:  ``{...}\\n{...}``

Plus the call-site behavior: multiple stories from a single chunk are
all processed; per-story required-field issues are warned and skipped
without halting the chunk; whole-chunk parse failures still emit the
existing ``story_extraction_parse_failed`` artifact.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker, StoryExtractor
from spectrum_systems_core.extraction.story_extractor import (
    PROMPT_TEMPLATE,
    _parse_story_response,
)

from ._fixtures import id_from_prompt, read_jsonl, write_text_units


def _story_payload(excerpt: str, chunk_id: str) -> dict:
    return {
        "story_found": True,
        "source_excerpt": excerpt,
        "story_summary": "A short summary of the moment that mattered most.",
        "possible_theme": "agency comments",
        "tier_guess": "tier_1",
        "why_it_might_work": "Because there is a clear human moment at stake.",
        "risk_flags": [],
        "source_turn_ids": [chunk_id],
    }


class ParseStoryResponseTests(unittest.TestCase):
    """Direct unit tests on ``_parse_story_response``."""

    def test_format_a_array_of_two_stories(self) -> None:
        raw = json.dumps(
            [{"story_found": True, "a": 1}, {"story_found": True, "a": 2}]
        )
        result = _parse_story_response(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["a"], 1)
        self.assertEqual(result[1]["a"], 2)

    def test_format_b_single_object_no_array(self) -> None:
        raw = json.dumps({"story_found": True, "a": 1})
        result = _parse_story_response(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["a"], 1)

    def test_format_c_concatenated_objects(self) -> None:
        raw = (
            json.dumps({"story_found": True, "a": 1})
            + "\n"
            + json.dumps({"story_found": True, "a": 2})
            + "\n"
            + json.dumps({"story_found": True, "a": 3})
        )
        result = _parse_story_response(raw)
        self.assertEqual(len(result), 3)
        self.assertEqual([r["a"] for r in result], [1, 2, 3])

    def test_format_c_with_various_whitespace(self) -> None:
        raw = (
            '{"a":1}'
            + "   \n\n   "
            + '{"a":2}'
            + "\t"
            + '{"a":3}'
        )
        result = _parse_story_response(raw)
        self.assertEqual(len(result), 3)

    def test_empty_array_returns_empty_list(self) -> None:
        self.assertEqual(_parse_story_response("[]"), [])

    def test_completely_malformed_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            _parse_story_response("this is not JSON at all")

    def test_mixed_valid_then_invalid_concatenated(self) -> None:
        # First object parses; trailing garbage cuts the scan short.
        raw = '{"a":1}\n{"a":2}\nnot json here'
        result = _parse_story_response(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual([r["a"] for r in result], [1, 2])

    def test_parse_function_is_importable_from_module(self) -> None:
        from spectrum_systems_core.extraction import story_extractor

        self.assertTrue(hasattr(story_extractor, "_parse_story_response"))
        self.assertIs(story_extractor._parse_story_response, _parse_story_response)


class PromptTemplateTests(unittest.TestCase):
    def test_prompt_template_requests_json_array(self) -> None:
        self.assertIn("Return a JSON array", PROMPT_TEMPLATE)


class CallSiteMultiStoryTests(unittest.TestCase):
    """End-to-end behavior of the extractor with multi-story responses."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-multi-001"
        texts = [
            "Paragraph one introduces the central question.",
            "Paragraph two describes a difficult moment when the team realized risk.",
            "Paragraph three closes with an uncertain decision.",
            "Paragraph four adds context.",
            "Paragraph five sets up the agency comments segment.",
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

    def test_call_site_iterates_over_array_response(self) -> None:
        """An array of 2 valid stories from one chunk produces 2 candidates."""
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            chunk_id = id_from_prompt(prompt, "Chunk ID")
            return json.dumps(
                [
                    _story_payload(first_excerpt, chunk_id),
                    _story_payload(first_excerpt, chunk_id),
                ]
            )

        result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        # Each chunk yielded 2 candidates.
        self.assertEqual(len(result["candidates"]), 2 * len(chunks))

    def test_call_site_handles_concatenated_objects(self) -> None:
        """Format C responses still produce candidates after the parser fix."""
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            chunk_id = id_from_prompt(prompt, "Chunk ID")
            return (
                json.dumps(_story_payload(first_excerpt, chunk_id))
                + "\n"
                + json.dumps(_story_payload(first_excerpt, chunk_id))
            )

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        # Each chunk yielded 2 candidates and zero parse_error blocks.
        self.assertEqual(len(result["candidates"]), 2 * len(chunks))
        self.assertEqual(extractor.last_run_counters.block_reasons["parse_error"], 0)

    def test_empty_array_produces_no_candidates_no_blocked(self) -> None:
        """``[]`` is the normal 'no stories' outcome — not a parse error."""
        def caller(prompt: str) -> str:
            return "[]"

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["candidates"]), 0)
        self.assertEqual(self._load_candidates(), [])
        self.assertEqual(
            extractor.last_run_counters.block_reasons["parse_error"], 0
        )
        self.assertEqual(
            extractor.last_run_counters.block_reasons["empty_response"], 0
        )

    def test_story_missing_required_fields_logs_warning_and_continues(self) -> None:
        """Per-story missing required fields → warn finding, chunk continues."""
        chunks = self._load_chunks()
        first_excerpt = chunks[0]["text"].split("\n", 1)[0]

        def caller(prompt: str) -> str:
            chunk_id = id_from_prompt(prompt, "Chunk ID")
            good = _story_payload(first_excerpt, chunk_id)
            bad = dict(good)
            bad["source_turn_ids"] = []  # missing required field
            return json.dumps([bad, good])

        extractor = StoryExtractor(api_caller=caller)
        with self.assertLogs(
            "spectrum_systems_core.extraction.story_extractor",
            level="WARNING",
        ) as cm:
            result = extractor.extract_from_source(
                self.source_id, str(self.repo_root)
            )
        self.assertEqual(result["status"], "success")
        # The valid story still landed even though its sibling was skipped.
        self.assertEqual(len(result["candidates"]), len(chunks))
        self.assertTrue(
            any("story_missing_required_fields" in m for m in cm.output),
            f"expected story_missing_required_fields warning, got: {cm.output}",
        )

    def test_completely_malformed_response_blocks_chunk(self) -> None:
        """Malformed response still flows through story_extraction_parse_failed."""
        def caller(prompt: str) -> str:
            return "absolutely not JSON"

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        records = self._load_candidates()
        self.assertGreater(len(records), 0)
        for rec in records:
            self.assertEqual(rec["status"], "blocked")
            self.assertIn("json_parse_error", rec["block_reason"])
        counters = extractor.last_run_counters
        self.assertEqual(
            counters.block_reasons["parse_error"], counters.chunks_attempted
        )


if __name__ == "__main__":
    unittest.main()
