"""Tests for StoryEval (Phase C, Step 6 + Red Team #2-001)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction import Chunker, StoryEval, StoryExtractor

from ._fixtures import id_from_prompt, read_jsonl, write_text_units


def _make_response(excerpt: str, prompt: str) -> str:
    return json.dumps(
        {
            "story_found": True,
            "source_excerpt": excerpt,
            "story_summary": "A story about a difficult moment of decision making.",
            "possible_theme": "decision under uncertainty",
            "tier_guess": "tier_1",
            "why_it_might_work": "It captures stakes and a five-second moment.",
            "risk_flags": [],
            "source_turn_ids": [id_from_prompt(prompt, "Chunk ID")],
        }
    )


class StoryEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "ns-eval-001"
        texts = [
            "We faced a difficult moment when the agency comments arrived.",
            "Suddenly the team realized the central question had changed.",
            "There was a clear risk if we delayed the decision.",
            "The stakes felt real as the deadline approached.",
            "We chose to act quickly despite uncertainty.",
            "Our struggle to align stakeholders revealed the cost.",
            "The room fell quiet as the choice was named.",
            "A single word ended the debate.",
            "The next morning, the consequences became clear.",
            "We learned that vulnerability builds trust.",
        ]
        write_text_units(
            self.repo_root, family="notes", source_id=self.source_id, texts=texts
        )
        Chunker().chunk(self.source_id, str(self.repo_root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load_candidates(self) -> list[dict[str, Any]]:
        path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "candidates.jsonl"
        )
        return read_jsonl(path)

    def _run_extractor(self, excerpt: str) -> list[dict[str, Any]]:
        result = StoryExtractor(
            api_caller=lambda p: _make_response(excerpt, p)
        ).extract_from_source(self.source_id, str(self.repo_root))
        return result["all_records"]

    def test_grounded_excerpt_passes_eval(self) -> None:
        # Choose a verbatim excerpt from text_units.
        verbatim = "We faced a difficult moment when the agency comments arrived."
        records = self._run_extractor(verbatim)
        eval_result = StoryEval().run(
            records, self.source_id, str(self.repo_root)
        )
        self.assertGreater(eval_result["pass_count"], 0)
        # All non-blocked candidates have grounded=True.
        for c in records:
            if c.get("status") == "candidate":
                self.assertTrue(c["grounded"])
                self.assertGreater(len(c["grounded_unit_ids"]), 0)

    def test_ungrounded_excerpt_blocked(self) -> None:
        # Hallucinated excerpt — never appears in text_units.
        hallucinated = "On Mars, we found extraterrestrial life forms in 2099."
        records = self._run_extractor(hallucinated)
        StoryEval().run(records, self.source_id, str(self.repo_root))
        # Reload from disk to confirm persistence.
        on_disk = self._load_candidates()
        self.assertEqual(len(on_disk), len(records))
        for c in on_disk:
            if c["chunk_id"] in {r["chunk_id"] for r in records}:
                # All these came from the same hallucinated excerpt.
                self.assertEqual(c["status"], "blocked")
                self.assertIn(
                    "excerpt_not_grounded_in_source", c["block_reason"]
                )

    def test_short_excerpt_blocked(self) -> None:
        # Less than 10 chars — fails before grounding.
        records = self._run_extractor("short")
        StoryEval().run(records, self.source_id, str(self.repo_root))
        # Either the extractor's schema validation or eval-004 must block.
        seen_blocked = False
        for c in records:
            if c.get("status") == "blocked":
                seen_blocked = True
                reason = c["block_reason"].lower()
                # Either reason is acceptable; both are debuggable.
                self.assertTrue(
                    "schema_violation" in reason
                    or "source_excerpt_too_short" in reason,
                    msg=f"unexpected block_reason: {reason}",
                )
        self.assertTrue(seen_blocked)

    def test_blocked_candidate_not_in_pass_count(self) -> None:
        records = self._run_extractor("On Mars we found life in the year 2099.")
        eval_result = StoryEval().run(
            records, self.source_id, str(self.repo_root)
        )
        # Hallucinated excerpts should give zero passes.
        self.assertEqual(eval_result["pass_count"], 0)
        self.assertGreater(eval_result["blocked_count"], 0)

    def test_grounding_exception_blocks_candidate(self) -> None:
        """RT5-003: a grounding helper exception must block, not pass."""

        class ExplodingGrounder:
            def verify_excerpt(self, *args, **kwargs):
                raise RuntimeError("boom")

        verbatim = "We faced a difficult moment when the agency comments arrived."
        records = self._run_extractor(verbatim)
        StoryEval(grounding=ExplodingGrounder()).run(
            records, self.source_id, str(self.repo_root)
        )
        for c in records:
            if c.get("chunk_id") and c.get("status") == "blocked":
                self.assertIn("grounding_check_failed", c["block_reason"])
                self.assertFalse(c["grounded"])


if __name__ == "__main__":
    unittest.main()
