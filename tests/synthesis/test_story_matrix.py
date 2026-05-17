"""Tests for StoryMatrix (audience-weighted deterministic story selection)."""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.story_matrix import (
    AUDIENCE_WEIGHT,
    StoryMatrix,
)

from ._fixtures import write_promoted_story


class StoryMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_story_selected_per_theme(self) -> None:
        write_promoted_story(
            self.repo_root,
            source_id="src-A",
            theme="adjacent channel interference modelling",
            tier_guess="tier_1",
        )
        themes = [{"theme_name": "adjacent channel interference modelling"}]
        result = StoryMatrix().build(
            run_id=str(uuid.uuid4()),
            audience="technical",
            themes=themes,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["matrix_entries"], 1)
        self.assertEqual(result["entries"][0]["audience"], "technical")

    def test_audience_weight_applied(self) -> None:
        # tier_3 story for executive audience => weight 0.1.
        write_promoted_story(
            self.repo_root,
            source_id="src-A",
            theme="rural broadband deployment funding",
            tier_guess="tier_3",
        )
        themes = [{"theme_name": "rural broadband deployment funding"}]
        result = StoryMatrix().build(
            run_id=str(uuid.uuid4()),
            audience="executive",
            themes=themes,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        # Score is jaccard*weight; tier_3 weight is 0.1.
        score = result["entries"][0]["relevance_score"]
        self.assertLessEqual(score, AUDIENCE_WEIGHT["executive"]["tier_3"])

    def test_no_promoted_stories_fails(self) -> None:
        themes = [{"theme_name": "anything goes"}]
        result = StoryMatrix().build(
            run_id=str(uuid.uuid4()),
            audience="technical",
            themes=themes,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("no_promoted_stories", result["reason"])

    def test_tier_1_preferred_for_executive_audience(self) -> None:
        write_promoted_story(
            self.repo_root,
            source_id="src-A",
            theme="urban spectrum allocation strategy",
            tier_guess="tier_1",
            story_id="11111111-1111-1111-1111-111111111111",
        )
        write_promoted_story(
            self.repo_root,
            source_id="src-B",
            theme="urban spectrum allocation strategy",
            tier_guess="tier_3",
            story_id="22222222-2222-2222-2222-222222222222",
        )
        themes = [{"theme_name": "urban spectrum allocation strategy"}]
        result = StoryMatrix().build(
            run_id=str(uuid.uuid4()),
            audience="executive",
            themes=themes,
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            result["entries"][0]["story_id"],
            "11111111-1111-1111-1111-111111111111",
        )

    def test_invalid_audience_fails(self) -> None:
        write_promoted_story(self.repo_root)
        result = StoryMatrix().build(
            run_id=str(uuid.uuid4()),
            audience="investor",
            themes=[],
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("invalid_audience", result["reason"])


if __name__ == "__main__":
    unittest.main()
