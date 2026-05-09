"""Tests for KeynoteEval (FINDING-F-005, fabrication blocks)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.keynote_eval import KeynoteEval

from ._fixtures import make_bundle, make_bundle_item


def _make_scaffold(
    *,
    bundle: dict,
    arc: list,
    opener_story_id: str | None = None,
    bundle_hash: str | None = None,
) -> dict:
    return {
        "scaffold_id": str(uuid.uuid4()),
        "run_id": bundle["run_id"],
        "bundle_id": bundle["bundle_id"],
        "bundle_hash": bundle_hash or bundle["bundle_hash"],
        "audience": "policy",
        "title": "Test Keynote",
        "opener": {
            "story_id": opener_story_id
            or (bundle["items"][0]["artifact_id"] if bundle["items"] else ""),
            "hook_text": "Open with a vivid scene and stakes.",
            "why_this_story": "It frames the question.",
        },
        "central_tension": "How do we balance access and interference?",
        "arc": arc,
        "closing_call_to_action": "Do this thing now.",
        "estimated_duration_minutes": 20,
        "generation_model": "claude-sonnet-4-20250514",
        "generation_temperature": 0,
        "status": "draft",
        "created_at": "2024-01-01T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("c" * 64),
        },
    }


class KeynoteEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.story = make_bundle_item(
            artifact_type="story_candidate",
            artifact_id=str(uuid.uuid4()),
        )
        self.claim = make_bundle_item(
            artifact_type="technical_claim",
            artifact_id=str(uuid.uuid4()),
            promoted_status="evidenced",
        )
        self.bundle = make_bundle(items=[self.story, self.claim, make_bundle_item()])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _full_arc(self) -> list:
        return [
            {
                "beat_type": "opener",
                "content": "Open with a story that grabs attention.",
                "story_id": self.story["artifact_id"],
                "claim_ids": [],
            },
            {
                "beat_type": "rising",
                "content": "Build the case with the data and tension.",
                "story_id": None,
                "claim_ids": [self.claim["artifact_id"]],
            },
            {
                "beat_type": "call_to_action",
                "content": "Ask the audience to take a specific step.",
                "story_id": None,
                "claim_ids": [],
            },
        ]

    def test_arc_fewer_than_3_beats_blocked(self) -> None:
        scaffold = _make_scaffold(bundle=self.bundle, arc=self._full_arc()[:2])
        result = KeynoteEval().run(scaffold, self.bundle, repo_root=str(self.repo_root))
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any("EVAL-KEY-003" in rc for rc in result["reason_codes"])
        )

    def test_missing_call_to_action_blocked(self) -> None:
        arc = [
            {
                "beat_type": "opener",
                "content": "Open the talk with the central scene.",
                "story_id": self.story["artifact_id"],
                "claim_ids": [],
            },
            {
                "beat_type": "rising",
                "content": "Build tension with claims and counterclaims.",
                "story_id": None,
                "claim_ids": [self.claim["artifact_id"]],
            },
            {
                "beat_type": "rising",
                "content": "Add another piece of context to deepen.",
                "story_id": None,
                "claim_ids": [],
            },
        ]
        scaffold = _make_scaffold(bundle=self.bundle, arc=arc)
        result = KeynoteEval().run(scaffold, self.bundle, repo_root=str(self.repo_root))
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any("EVAL-KEY-004" in rc for rc in result["reason_codes"])
        )

    def test_story_not_in_bundle_blocked(self) -> None:
        bogus = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        scaffold = _make_scaffold(
            bundle=self.bundle,
            arc=self._full_arc(),
            opener_story_id=bogus,
        )
        result = KeynoteEval().run(scaffold, self.bundle, repo_root=str(self.repo_root))
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any("EVAL-KEY-002" in rc for rc in result["reason_codes"])
        )

    def test_claim_not_in_bundle_blocked(self) -> None:
        bogus = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        arc = self._full_arc()
        arc[1]["claim_ids"] = [bogus]
        scaffold = _make_scaffold(bundle=self.bundle, arc=arc)
        result = KeynoteEval().run(scaffold, self.bundle, repo_root=str(self.repo_root))
        self.assertEqual(result["decision"], "block")
        self.assertTrue(
            any("EVAL-KEY-005" in rc for rc in result["reason_codes"])
        )

    def test_bundle_hash_mismatch_warns(self) -> None:
        # Plant a fake report draft on disk under our temp repo root.
        run_id = self.bundle["run_id"]
        run_dir = self.repo_root / "synthesis" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "draft_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bundle_id": self.bundle["bundle_id"],
            "bundle_hash": "sha256:" + ("d" * 64),
            "audience": "policy",
            "title": "Some Report",
            "sections": [
                {
                    "section_id": str(uuid.uuid4()),
                    "section_title": "Background",
                    "section_type": "background",
                    "content": "Content",
                    "inline_citations": [],
                    "grounded": False,
                    "unverified_citations": [],
                }
            ],
            "generation_model": "claude-sonnet-4-20250514",
            "generation_temperature": 0,
            "status": "draft",
            "created_at": "2024-01-01T00:00:00+00:00",
            "provenance": {
                "produced_by": {"component": "test", "version": "1.0.0"},
                "input_artifact_ids": [],
                "execution_fingerprint_hash": "sha256:" + ("e" * 64),
            },
        }
        (run_dir / "report_draft.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        scaffold = _make_scaffold(
            bundle=self.bundle,
            arc=self._full_arc(),
            bundle_hash="sha256:" + ("a" * 64),
        )
        result = KeynoteEval().run(
            scaffold, self.bundle, repo_root=str(self.repo_root)
        )
        # Mismatch should warn but not block (only EVAL-KEY-007 fires).
        self.assertEqual(result["decision"], "warn", result)
        self.assertTrue(
            any("EVAL-KEY-007" in wc for wc in result["warn_codes"])
        )

    def test_clean_scaffold_allowed(self) -> None:
        scaffold = _make_scaffold(bundle=self.bundle, arc=self._full_arc())
        result = KeynoteEval().run(
            scaffold, self.bundle, repo_root=str(self.repo_root)
        )
        self.assertEqual(result["decision"], "allow", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
