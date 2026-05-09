"""Tests for ConnectionEngine (Phase C, Step 14 + Red Team #4)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

from spectrum_systems_core.extraction import ConnectionEngine

from ._fixtures import read_jsonl


def _make_story(
    *,
    source_id: str,
    theme: str = "agency comments review process",
    summary: str = "A summary that is over twenty characters in length.",
    why: str = "Because of the strong moment described here.",
) -> Dict[str, Any]:
    return {
        "story_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_family": "notes",
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [],
        "source_excerpt": "An excerpt that is grounded in the source text.",
        "story_summary": summary,
        "possible_theme": theme,
        "tier_guess": "tier_1",
        "why_it_might_work": why,
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


def _write_promoted(
    repo_root: Path, source_id: str, stories: List[Dict[str, Any]]
) -> None:
    target = repo_root / "processed" / "notes" / source_id / "stories" / "promoted"
    target.mkdir(parents=True, exist_ok=True)
    for s in stories:
        (target / f"{s['story_id']}.json").write_text(
            json.dumps(s, sort_keys=True), encoding="utf-8"
        )


class ConnectionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_strong_connection_three_matching_fields(self) -> None:
        story_a = _make_story(
            source_id="src-A",
            theme="agency comments review process",
            summary="A shared summary text used to test connections.",
            why="A clear and shared rationale across both sources here.",
        )
        story_b = _make_story(
            source_id="src-B",
            theme="agency comments review process",
            summary="A shared summary text used to test connections.",
            why="A clear and shared rationale across both sources here.",
        )
        _write_promoted(self.repo_root, "src-A", [story_a])
        _write_promoted(self.repo_root, "src-B", [story_b])
        result = ConnectionEngine().find_connections(str(self.repo_root))
        self.assertEqual(result["strong_count"], 1)
        path = (
            self.repo_root / "processed" / "notes" / "src-A"
            / "knowledge" / "connections.jsonl"
        )
        records = read_jsonl(path)
        self.assertEqual(records[0]["strength"], "strong")

    def test_moderate_connection_two_matching_fields(self) -> None:
        story_a = _make_story(
            source_id="src-A",
            theme="agency comments review process",
            summary="A shared summary text used to test connections.",
            why="Source A specific rationale that differs from B's.",
        )
        story_b = _make_story(
            source_id="src-B",
            theme="agency comments review process",
            summary="A shared summary text used to test connections.",
            why="Source B specific rationale that differs from A's.",
        )
        _write_promoted(self.repo_root, "src-A", [story_a])
        _write_promoted(self.repo_root, "src-B", [story_b])
        result = ConnectionEngine().find_connections(str(self.repo_root))
        self.assertEqual(result["moderate_count"], 1)
        self.assertEqual(result["strong_count"], 0)

    def test_weak_connection_not_written(self) -> None:
        story_a = _make_story(
            source_id="src-A",
            theme="agency comments review process",
            summary="A unique summary in source A only here.",
            why="Source A specific rationale that differs.",
        )
        story_b = _make_story(
            source_id="src-B",
            theme="agency comments review process",
            summary="A different summary in source B only here.",
            why="Source B specific rationale that differs.",
        )
        _write_promoted(self.repo_root, "src-A", [story_a])
        _write_promoted(self.repo_root, "src-B", [story_b])
        result = ConnectionEngine().find_connections(str(self.repo_root))
        self.assertEqual(result["weak_count"], 1)
        self.assertEqual(result["strong_count"] + result["moderate_count"], 0)
        path = (
            self.repo_root / "processed" / "notes" / "src-A"
            / "knowledge" / "connections.jsonl"
        )
        self.assertFalse(path.is_file())

    def test_same_source_not_connected(self) -> None:
        story_a = _make_story(source_id="src-A", theme="agency comments review")
        story_b = _make_story(source_id="src-A", theme="agency comments review")
        _write_promoted(self.repo_root, "src-A", [story_a, story_b])
        result = ConnectionEngine().find_connections(str(self.repo_root))
        self.assertEqual(result["strong_count"] + result["moderate_count"], 0)

    def test_short_field_value_not_matched(self) -> None:
        """RT4-002: 5-character shared 'risk' must not connect sources."""
        # All other fields differ; only possible_theme would match if not for
        # the 10-char rule — and it's only 4 chars.
        story_a = _make_story(
            source_id="src-A",
            theme="risk",
            summary="A unique source-A summary here that is unrelated.",
            why="Source-A unique rationale that does not match B at all.",
        )
        story_b = _make_story(
            source_id="src-B",
            theme="risk",
            summary="A unique source-B summary that is completely different.",
            why="Source-B unique rationale that does not overlap A's.",
        )
        _write_promoted(self.repo_root, "src-A", [story_a])
        _write_promoted(self.repo_root, "src-B", [story_b])
        result = ConnectionEngine().find_connections(str(self.repo_root))
        self.assertEqual(result["strong_count"] + result["moderate_count"], 0)
        # Even weak count is 0 — the only candidate field is "risk" which
        # is below the 10-char threshold and so isn't even considered.
        self.assertEqual(result["weak_count"], 0)


if __name__ == "__main__":
    unittest.main()
