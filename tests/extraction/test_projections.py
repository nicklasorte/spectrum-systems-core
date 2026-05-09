"""Tests for the Phase C ObsidianProjection extensions.

RT2-004: blocked candidates listed by ID + reason but excerpts NOT shown.
RT4-006: knowledge / connection projections are explicitly view-only.
RT5-004: no Markdown is read back as authority (search-based).
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

from spectrum_systems_core.ingestion import ObsidianProjection


def _make_candidate(*, status: str, story_id: str, excerpt: str) -> Dict[str, Any]:
    return {
        "story_id": story_id,
        "source_id": "src",
        "source_family": "notes",
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [1, 2],
        "source_excerpt": excerpt,
        "story_summary": "A summary that is over twenty characters in length.",
        "possible_theme": "trial themes",
        "tier_guess": "tier_1",
        "why_it_might_work": "Because of the strong moment described here.",
        "risk_flags": ["may need redaction"],
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
        "grounded": status != "blocked",
        "grounded_unit_ids": [],
        "status": status,
        "superseded_by": None,
        "created_at": "2026-05-09T00:00:00+00:00",
        "block_reason": "excerpt_not_grounded_in_source" if status == "blocked" else "",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("0" * 64),
        },
    }


class StoryProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        # Set up processed directory.
        (self.repo_root / "processed" / "notes" / "src").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_blocked_excerpt_not_in_projection(self) -> None:
        """RT2-004: blocked candidates' excerpts must not appear."""
        ok_id = str(uuid.uuid4())
        bad_id = str(uuid.uuid4())
        candidates: List[Dict[str, Any]] = [
            _make_candidate(
                status="candidate", story_id=ok_id,
                excerpt="A grounded excerpt that exists in source text.",
            ),
            _make_candidate(
                status="blocked", story_id=bad_id,
                excerpt="HALLUCINATED text that is not in the source.",
            ),
        ]
        path = ObsidianProjection().write_story_projection(
            "src", candidates, str(self.repo_root), label="post-eval"
        )
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn(ok_id, body)
        self.assertIn(bad_id, body)
        self.assertIn("A grounded excerpt", body)
        # Hallucinated excerpt MUST NOT appear in the projection.
        self.assertNotIn("HALLUCINATED text", body)
        self.assertIn("excerpt_not_grounded_in_source", body)

    def test_projection_marked_view_only(self) -> None:
        path = ObsidianProjection().write_story_projection(
            "src", [], str(self.repo_root), label="post-eval"
        )
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn("vault_note_status: projection", body)
        self.assertIn("VIEW ONLY", body)


class KnowledgeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        sid = "src-k"
        target = self.repo_root / "processed" / "notes" / sid / "knowledge"
        target.mkdir(parents=True)
        with (target / "concepts.jsonl").open("w") as fh:
            fh.write(json.dumps({
                "concept_id": str(uuid.uuid4()),
                "concept_name": "concept name here",
                "definition": "A definition that is over twenty chars long.",
                "source_story_ids": [str(uuid.uuid4())],
                "source_ids": [sid],
                "supporting_excerpts": [
                    {"unit_id": "u-1", "excerpt": "snippet", "source_id": sid}
                ],
                "related_concepts": [],
                "status": "candidate",
                "created_at": "2026-05-09T00:00:00+00:00",
            }) + "\n")
        self.sid = sid

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_knowledge_projection_view_only(self) -> None:
        """RT4-006: knowledge projection is view-only."""
        path = ObsidianProjection().write_knowledge_projection(
            self.sid, str(self.repo_root), label="post-synthesis"
        )
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn("vault_note_status: projection", body)
        self.assertIn("VIEW ONLY", body)

    def test_connection_projection_view_only(self) -> None:
        path = ObsidianProjection().write_connection_projection(
            self.sid, str(self.repo_root), label="post-connections"
        )
        body = Path(path).read_text(encoding="utf-8")
        self.assertIn("vault_note_status: projection", body)
        self.assertIn("VIEW ONLY", body)


class MarkdownAuthorityLeakTests(unittest.TestCase):
    """RT5-004: no code reads from stories.md, knowledge.md, or
    connections.md as input. Search the codebase for any open() of a Phase
    C projection file outside the projection writers.
    """

    def test_no_markdown_read_back(self) -> None:
        """Search for open()/read_* of any projection file outside the writers.

        Help text and docstrings that mention the filenames are fine — what
        we forbid is code that loads them as input. We approximate that by
        checking each line that mentions a projection filename: if the same
        line also calls .read_text / open / Path(...).read_*, flag it.
        """
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders: List[str] = []
        needles = ("stories.md", "knowledge.md", "connections.md")
        for path in src_root.rglob("*.py"):
            if path.name == "obsidian_projection.py":
                continue
            text = path.read_text(encoding="utf-8")
            for line_num, line in enumerate(text.splitlines(), start=1):
                if not any(n in line for n in needles):
                    continue
                low = line.lower()
                if (
                    ".read_text" in low
                    or ".read_bytes" in low
                    or "open(" in low
                    or "load(" in low
                ):
                    offenders.append(f"{path}:{line_num}: {line.strip()}")
        self.assertEqual(
            offenders, [], msg=f"markdown read-back leak: {offenders}"
        )


if __name__ == "__main__":
    unittest.main()
