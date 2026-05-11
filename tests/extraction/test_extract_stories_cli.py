"""Integration smoke test for the extract-stories CLI flow.

Mocks the Anthropic API (StoryExtractor accepts an api_caller in unit tests
but the CLI wires the production client). Here we exercise the public
pipeline end-to-end by manually running each phase the CLI calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import (
    Chunker,
    StoryEval,
    StoryExtractor,
    StoryworthyFilter,
)
from spectrum_systems_core.ingestion import ObsidianProjection

from ._fixtures import id_from_prompt, write_text_units


class ExtractStoriesIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "src-pipeline-001"
        self.texts = [
            "We faced a difficult moment when the central question came up.",
            "Suddenly the team realized risk and the stakes were clear.",
            "Then we decided how to proceed under uncertainty.",
            "A clear five-second moment changed the room.",
            "The cost would be a failure if we delayed the decision.",
            "We struggled to align stakeholders on the way forward.",
            "The next morning the consequences became visible.",
            "We learned that vulnerability builds trust over time.",
        ]
        write_text_units(
            self.repo_root, family="notes", source_id=self.source_id,
            texts=self.texts,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_pipeline_grounded_admit_candidate(self) -> None:
        # 1) Chunker
        chunk_result = Chunker().chunk(self.source_id, str(self.repo_root))
        self.assertEqual(chunk_result["status"], "success")

        # 2) Extractor (mocked) — return a verbatim excerpt that exists.
        verbatim = self.texts[1]

        def caller(prompt: str) -> str:
            return json.dumps(
                {
                    "story_found": True,
                    "source_excerpt": verbatim,
                    "story_summary": (
                        "When the moment arrived suddenly the team realized "
                        "risk and chose how to proceed."
                    ),
                    "possible_theme": "decision under uncertainty",
                    "tier_guess": "tier_1",
                    "why_it_might_work": (
                        "It captures a five-second moment, stakes, and a "
                        "central question of whether to proceed."
                    ),
                    "risk_flags": [],
                    "source_turn_ids": [id_from_prompt(prompt, "Chunk ID")],
                }
            )

        extract_result = StoryExtractor(api_caller=caller).extract_from_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(extract_result["status"], "success")
        records = extract_result["all_records"]
        self.assertGreater(len(records), 0)

        # 3) Eval
        eval_result = StoryEval().run(
            records, self.source_id, str(self.repo_root)
        )
        self.assertGreater(eval_result["pass_count"], 0)

        # 4) Storyworthy filter
        filter_result = StoryworthyFilter().run_on_source(
            self.source_id, str(self.repo_root)
        )
        self.assertEqual(filter_result["status"], "success")
        self.assertGreater(filter_result["scored_count"], 0)

        # 5) Projection
        proj_path = ObsidianProjection().write_story_projection(
            self.source_id, filter_result["candidates"], str(self.repo_root),
            label="post-eval",
        )
        self.assertTrue(Path(proj_path).is_file())
        body = Path(proj_path).read_text(encoding="utf-8")
        self.assertIn("Story Bank", body)


if __name__ == "__main__":
    unittest.main()
