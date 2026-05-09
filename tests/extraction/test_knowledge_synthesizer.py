"""Tests for KnowledgeSynthesizer (Phase C, Step 12 + Red Team #4)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

from spectrum_systems_core.extraction import KnowledgeSynthesizer

from ._fixtures import read_jsonl


def _make_promoted_story(
    *,
    source_id: str,
    theme: str,
    tier: str = "tier_1",
    summary: str = "A summary that is over twenty characters in length.",
    excerpt: str = "An excerpt that is grounded in the source.",
) -> Dict[str, Any]:
    return {
        "story_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_family": "notes",
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [],
        "source_excerpt": excerpt,
        "story_summary": summary,
        "possible_theme": theme,
        "tier_guess": tier,
        "why_it_might_work": "Because of the strong moment described here.",
        "risk_flags": [],
        "storyworthy_score": {
            "five_second_moment": 3,
            "stakes": 3,
            "central_question": 2,
            "vulnerability": 0,
            "narrative_compression": 3,
            "total": 11,
        },
        "storyworthy_verdict": "admit",
        "extraction_model": "claude-haiku-4-5-20251001",
        "extraction_temperature": 0,
        "grounded": True,
        "grounded_unit_ids": [str(uuid.uuid4())],
        "status": "promoted",
        "superseded_by": None,
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("0" * 64),
        },
    }


def _write_promoted_stories(
    repo_root: Path, source_id: str, stories: List[Dict[str, Any]]
) -> None:
    target = repo_root / "processed" / "notes" / source_id / "stories" / "promoted"
    target.mkdir(parents=True, exist_ok=True)
    for story in stories:
        (target / f"{story['story_id']}.json").write_text(
            json.dumps(story, sort_keys=True) + "\n", encoding="utf-8"
        )


class KnowledgeSynthesizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concepts_from_themed_stories(self) -> None:
        sid = "src-001"
        stories = [
            _make_promoted_story(
                source_id=sid, theme="agency comments review process"
            ),
            _make_promoted_story(
                source_id=sid, theme="agency comments review process"
            ),
        ]
        _write_promoted_stories(self.repo_root, sid, stories)
        result = KnowledgeSynthesizer().synthesize_concepts(
            sid, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["concept_count"], 1)
        path = (
            self.repo_root / "processed" / "notes" / sid
            / "knowledge" / "concepts.jsonl"
        )
        records = read_jsonl(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "candidate")  # RT4-006

    def test_no_concepts_from_single_story(self) -> None:
        sid = "src-002"
        stories = [
            _make_promoted_story(
                source_id=sid, theme="lonely theme phrase"
            )
        ]
        _write_promoted_stories(self.repo_root, sid, stories)
        result = KnowledgeSynthesizer().synthesize_concepts(
            sid, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["concept_count"], 0)

    def test_concept_requires_supporting_excerpt(self) -> None:
        """RT4-001: concepts without grounded excerpts must be skipped."""
        sid = "src-003"
        # Stories without source_excerpt — will fail to produce excerpts.
        s1 = _make_promoted_story(
            source_id=sid, theme="theme without excerpts"
        )
        s2 = _make_promoted_story(
            source_id=sid, theme="theme without excerpts"
        )
        s1["source_excerpt"] = ""
        s2["source_excerpt"] = ""
        s1["grounded_unit_ids"] = []
        s2["grounded_unit_ids"] = []
        _write_promoted_stories(self.repo_root, sid, [s1, s2])
        result = KnowledgeSynthesizer().synthesize_concepts(
            sid, str(self.repo_root)
        )
        self.assertEqual(result["concept_count"], 0)

    def test_analogies_skipped_with_one_source(self) -> None:
        """RT4-005: with only 1 source, analogies must skip silently."""
        sid = "src-004"
        stories = [_make_promoted_story(source_id=sid, theme="solo theme")]
        _write_promoted_stories(self.repo_root, sid, stories)
        result = KnowledgeSynthesizer().synthesize_analogies(
            sid, str(self.repo_root)
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["analogy_count"], 0)
        self.assertIn("insufficient_sources", result["reason"])

    def test_all_synthesis_artifacts_candidate_status(self) -> None:
        """FINDING-C-003: synthesis artifacts must always be candidate status."""
        sid = "src-005"
        stories = [
            _make_promoted_story(source_id=sid, theme="theme of importance"),
            _make_promoted_story(source_id=sid, theme="theme of importance"),
        ]
        _write_promoted_stories(self.repo_root, sid, stories)
        synth = KnowledgeSynthesizer()
        synth.synthesize_concepts(sid, str(self.repo_root))
        synth.synthesize_themes(sid, str(self.repo_root))
        for filename in ("concepts.jsonl", "themes.jsonl"):
            path = (
                self.repo_root / "processed" / "notes" / sid
                / "knowledge" / filename
            )
            for record in read_jsonl(path):
                self.assertEqual(record["status"], "candidate")

    def test_themes_only_from_tier_1(self) -> None:
        sid = "src-006"
        stories = [
            _make_promoted_story(
                source_id=sid, theme="critical theme x", tier="tier_1"
            ),
            _make_promoted_story(
                source_id=sid, theme="background theme y", tier="tier_3"
            ),
        ]
        _write_promoted_stories(self.repo_root, sid, stories)
        result = KnowledgeSynthesizer().synthesize_themes(
            sid, str(self.repo_root)
        )
        self.assertEqual(result["theme_count"], 1)


if __name__ == "__main__":
    unittest.main()
