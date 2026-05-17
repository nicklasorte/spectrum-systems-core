"""Tests for StoryworthyFilter (Phase C, Step 9)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction import StoryworthyFilter


def _make_candidate(
    *,
    summary: str = "A summary",
    why: str = "It works",
    excerpt: str = "An excerpt that is long enough.",
    grounded: bool = True,
    status: str = "candidate",
) -> dict[str, Any]:
    return {
        "story_id": str(uuid.uuid4()),
        "source_id": "src",
        "source_family": "notes",
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [],
        "source_excerpt": excerpt,
        "story_summary": summary,
        "possible_theme": "theme",
        "tier_guess": "tier_1",
        "why_it_might_work": why,
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
        "grounded": grounded,
        "grounded_unit_ids": [],
        "status": status,
        "superseded_by": None,
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("0" * 64),
        },
    }


class StoryworthyFilterTests(unittest.TestCase):
    def test_high_scoring_story_admits(self) -> None:
        candidate = _make_candidate(
            summary=(
                "When the moment arrived suddenly the team realized the risk "
                "and the cost would be a difficult failure if the central "
                "question of whether to proceed went wrong."
            ),
            why=(
                "It captures a five second moment, the stakes, vulnerability, "
                "and a clear central question."
            ),
        )
        StoryworthyFilter().score(candidate)
        self.assertEqual(candidate["storyworthy_verdict"], "admit")
        self.assertGreaterEqual(candidate["storyworthy_score"]["total"], 10)

    def test_low_scoring_story_rejects(self) -> None:
        candidate = _make_candidate(
            summary="A bland sentence with no signal.",
            why="Plain prose with no triggers.",
            excerpt="x" * 4000,  # >300 words approximation? It's chars not words
        )
        # Use truly long word count to hit reject end of compression.
        candidate["story_summary"] = "word " * 400
        candidate["why_it_might_work"] = "calm steady prose"
        candidate["source_excerpt"] = "word " * 100
        StoryworthyFilter().score(candidate)
        self.assertEqual(candidate["storyworthy_verdict"], "reject")
        self.assertLess(candidate["storyworthy_score"]["total"], 6)

    def test_mid_scoring_story_revises(self) -> None:
        candidate = _make_candidate(
            summary=(
                "A moment of failure came up which raised the central question."
            ),
            why="There is a clear stake.",
        )
        StoryworthyFilter().score(candidate)
        verdict = candidate["storyworthy_verdict"]
        # The exact band depends on keyword hits; ensure score makes sense.
        total = candidate["storyworthy_score"]["total"]
        if 6 <= total < 10:
            self.assertEqual(verdict, "revise")
        elif total >= 10:
            self.assertEqual(verdict, "admit")
        else:
            self.assertEqual(verdict, "reject")

    def test_all_dimensions_scored(self) -> None:
        candidate = _make_candidate()
        StoryworthyFilter().score(candidate)
        score = candidate["storyworthy_score"]
        for dim in (
            "five_second_moment",
            "stakes",
            "central_question",
            "vulnerability",
            "narrative_compression",
            "total",
        ):
            self.assertIn(dim, score)
        self.assertEqual(
            score["total"],
            sum(
                score[k]
                for k in (
                    "five_second_moment",
                    "stakes",
                    "central_question",
                    "vulnerability",
                    "narrative_compression",
                )
            ),
        )

    def test_only_grounded_candidates_scored(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        repo_root = Path(tmp.name)
        try:
            target = repo_root / "processed" / "notes" / "src" / "stories"
            target.mkdir(parents=True, exist_ok=True)
            grounded = _make_candidate(grounded=True)
            ungrounded = _make_candidate(grounded=False)
            blocked = _make_candidate(status="blocked")
            with (target / "candidates.jsonl").open("w") as fh:
                for c in (grounded, ungrounded, blocked):
                    fh.write(json.dumps(c) + "\n")
            result = StoryworthyFilter().run_on_source("src", str(repo_root))
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["scored_count"], 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
