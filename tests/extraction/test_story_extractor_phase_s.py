"""Phase S.0: guard_empty_response + strip_markdown_fence in story extraction.

Mirrors the Phase X call order (guard_empty_response -> strip_markdown_fence ->
json.loads) for the story_extractor path. The five tests below assert:

1. An empty / whitespace-only response emits
   ``story_extraction_empty_response`` and halts that chunk.
2. A fenced JSON body parses correctly (fences are stripped before parse).
3. Malformed JSON after fence strip emits ``story_extraction_parse_failed``.
4. Both failure modes count toward ``chunks_blocked`` and bump the matching
   block_reason on the runner counter.
5. The Phase X resilience primitives are imported (not duplicated) by the
   story extractor module.
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker, StoryExtractor
from spectrum_systems_core.extraction import _resilience as resilience
from spectrum_systems_core.extraction import story_extractor as story_module
from spectrum_systems_core.extraction._failure_artifacts import (
    ARTIFACT_STORY_EMPTY_RESPONSE,
    ARTIFACT_STORY_PARSE_FAILED,
)

from ._fixtures import write_text_units

_FENCED_OK = (
    "```json\n"
    "{\n"
    "  \"story_found\": false,\n"
    "  \"source_excerpt\": null,\n"
    "  \"story_summary\": null,\n"
    "  \"possible_theme\": null,\n"
    "  \"tier_guess\": null,\n"
    "  \"why_it_might_work\": null,\n"
    "  \"risk_flags\": []\n"
    "}\n"
    "```\n"
)


class StoryExtractorPhaseSTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-phase-s-001"
        texts = [
            "First paragraph kicks off the discussion of the proposed rule.",
            "Second paragraph dives into a critical engineering decision.",
            "Third paragraph captures an unresolved question from the floor.",
            "Fourth paragraph notes a side observation from a participant.",
            "Fifth paragraph wraps up the agenda item.",
        ]
        write_text_units(
            self.repo_root, family="notes", source_id=self.source_id, texts=texts
        )
        Chunker().chunk(self.source_id, str(self.repo_root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _candidates_path(self) -> Path:
        return (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "candidates.jsonl"
        )

    def _read_blocked(self) -> list[dict]:
        import json as _json
        out = []
        for line in self._candidates_path().read_text(
            encoding="utf-8"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(_json.loads(line))
        return out

    def test_empty_response_emits_story_extraction_empty_response(self) -> None:
        """S.0 unit 1: empty/whitespace response halts the chunk and is
        counted as empty_response."""
        def caller(prompt: str) -> str:
            return "   \n   "

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(self.source_id, str(self.repo_root))

        self.assertEqual(result["status"], "success")
        # Every chunk halted -> all are blocked candidates with the
        # empty_response block_reason on the artifact.
        records = self._read_blocked()
        self.assertGreater(len(records), 0)
        for rec in records:
            self.assertEqual(rec["status"], "blocked")
            self.assertTrue(
                rec["block_reason"].startswith("empty_response:"),
                f"unexpected block_reason: {rec['block_reason']}",
            )
        # Counter must reflect empty_response blocks.
        counters = extractor.last_run_counters
        self.assertEqual(counters.chunks_succeeded, 0)
        self.assertEqual(
            counters.chunks_blocked, counters.chunks_attempted
        )
        self.assertEqual(
            counters.block_reasons["empty_response"],
            counters.chunks_attempted,
        )

    def test_fenced_json_parses_correctly(self) -> None:
        """S.0 unit 2: markdown-fenced JSON is stripped before parse."""
        def caller(prompt: str) -> str:
            return _FENCED_OK

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(self.source_id, str(self.repo_root))

        self.assertEqual(result["status"], "success")
        # story_found=False produces zero blocked records and zero candidates
        # but does NOT bump empty_response or parse_error.
        counters = extractor.last_run_counters
        self.assertEqual(counters.block_reasons["empty_response"], 0)
        self.assertEqual(counters.block_reasons["parse_error"], 0)
        # Every chunk produced a non-error outcome.
        self.assertEqual(counters.chunks_blocked, 0)

    def test_malformed_json_emits_story_extraction_parse_failed(self) -> None:
        """S.0 unit 3: non-empty, unparseable response emits parse_failed."""
        def caller(prompt: str) -> str:
            return "this is not JSON {"

        extractor = StoryExtractor(api_caller=caller)
        result = extractor.extract_from_source(self.source_id, str(self.repo_root))

        self.assertEqual(result["status"], "success")
        records = self._read_blocked()
        self.assertGreater(len(records), 0)
        for rec in records:
            self.assertEqual(rec["status"], "blocked")
            self.assertIn("json_parse_error", rec["block_reason"])
        counters = extractor.last_run_counters
        self.assertEqual(
            counters.block_reasons["parse_error"],
            counters.chunks_attempted,
        )

    def test_both_failure_modes_count_toward_chunks_blocked(self) -> None:
        """S.0 unit 4: empty_response AND parse_error sum into chunks_blocked
        with the matching per-reason tallies."""
        # First run: empty response.
        empty_extractor = StoryExtractor(api_caller=lambda p: "")
        empty_extractor.extract_from_source(self.source_id, str(self.repo_root))
        empty_counters = empty_extractor.last_run_counters
        self.assertEqual(
            empty_counters.chunks_blocked,
            empty_counters.block_reasons["empty_response"],
        )
        # Second run: parse error.
        parse_extractor = StoryExtractor(api_caller=lambda p: "{not json}")
        parse_extractor.extract_from_source(self.source_id, str(self.repo_root))
        parse_counters = parse_extractor.last_run_counters
        self.assertEqual(
            parse_counters.chunks_blocked,
            parse_counters.block_reasons["parse_error"],
        )
        # Across both runs, every chunk-attempt landed in chunks_blocked.
        self.assertGreater(empty_counters.chunks_blocked, 0)
        self.assertGreater(parse_counters.chunks_blocked, 0)

    def test_phase_x_functions_imported_not_duplicated(self) -> None:
        """S.0 unit 5: ``guard_empty_response`` and ``strip_markdown_fence``
        are imported from ``_resilience`` (not redefined). Importing the
        same symbol from two modules and asserting object identity proves
        there is exactly one implementation."""
        self.assertIs(
            story_module.guard_empty_response,
            resilience.guard_empty_response,
        )
        self.assertIs(
            story_module.strip_markdown_fence,
            resilience.strip_markdown_fence,
        )
        # The story_extractor source must not redefine the function names
        # locally (a top-level ``def`` would shadow the import).
        src = inspect.getsource(story_module)
        for name in ("guard_empty_response", "strip_markdown_fence"):
            self.assertNotIn(
                f"def {name}(",
                src,
                f"story_extractor.py must import {name}, not redefine it",
            )

    def test_story_failure_artifact_types_are_distinct(self) -> None:
        """Phase S.0 must use story_extraction_* artifact_types (not the
        typed_extraction_* ones) so the forensic record names the failing
        component."""
        self.assertEqual(
            ARTIFACT_STORY_EMPTY_RESPONSE, "story_extraction_empty_response"
        )
        self.assertEqual(
            ARTIFACT_STORY_PARSE_FAILED, "story_extraction_parse_failed"
        )


if __name__ == "__main__":
    unittest.main()
