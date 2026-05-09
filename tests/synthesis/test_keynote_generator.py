"""Tests for KeynoteGenerator (mocked Sonnet, FINDING-F-002, FINDING-F-005)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.keynote_generator import KeynoteGenerator

from ._fixtures import make_bundle, make_bundle_item


def _scaffold_json(*, opener_story_id: str, claim_id: str) -> str:
    return json.dumps(
        {
            "title": "Spectrum Strategy Keynote",
            "opener": {
                "story_id": opener_story_id,
                "hook_text": "A story about a critical regulatory moment.",
                "why_this_story": "It frames the question for everyone.",
            },
            "central_tension": "How do we balance access and interference?",
            "arc": [
                {
                    "beat_type": "opener",
                    "content": "Set the stage with the opening anecdote.",
                    "story_id": opener_story_id,
                    "claim_ids": [],
                },
                {
                    "beat_type": "rising",
                    "content": "Lay out the data and constraints involved.",
                    "story_id": None,
                    "claim_ids": [claim_id],
                },
                {
                    "beat_type": "call_to_action",
                    "content": "Ask the audience to act on the recommendation.",
                    "story_id": None,
                    "claim_ids": [],
                },
            ],
            "closing_call_to_action": "Vote yes on the proposal.",
            "estimated_duration_minutes": 18,
        }
    )


class KeynoteGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.run_id = str(uuid.uuid4())
        self.story_item = make_bundle_item(
            artifact_type="story_candidate",
            artifact_id=str(uuid.uuid4()),
        )
        self.claim_item = make_bundle_item(
            artifact_type="technical_claim",
            artifact_id=str(uuid.uuid4()),
            promoted_status="evidenced",
        )
        self.theme_item = make_bundle_item(
            artifact_type="theme_record",
            artifact_id=str(uuid.uuid4()),
        )
        self.bundle = make_bundle(
            run_id=self.run_id,
            items=[self.story_item, self.claim_item, self.theme_item],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_scaffold_generated(self) -> None:
        text = _scaffold_json(
            opener_story_id=self.story_item["artifact_id"],
            claim_id=self.claim_item["artifact_id"],
        )
        gen = KeynoteGenerator(api_caller=lambda _p: (text, 200, 100))
        result = gen.generate(
            self.run_id, self.bundle, "policy",
            {"entries": []}, str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        path = (
            self.repo_root / "synthesis" / self.run_id / "keynote_scaffold.json"
        )
        self.assertTrue(path.is_file())
        scaffold = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(scaffold["bundle_hash"], self.bundle["bundle_hash"])

    def test_fabricated_story_id_blocked(self) -> None:
        bogus = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        text = _scaffold_json(
            opener_story_id=bogus,
            claim_id=self.claim_item["artifact_id"],
        )
        gen = KeynoteGenerator(api_caller=lambda _p: (text, 100, 50))
        result = gen.generate(
            self.run_id, self.bundle, "policy",
            {"entries": []}, str(self.repo_root),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("fabricated_story_id", result["reason"])

    def test_fabricated_claim_id_blocked(self) -> None:
        bogus = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        text = _scaffold_json(
            opener_story_id=self.story_item["artifact_id"],
            claim_id=bogus,
        )
        gen = KeynoteGenerator(api_caller=lambda _p: (text, 100, 50))
        result = gen.generate(
            self.run_id, self.bundle, "policy",
            {"entries": []}, str(self.repo_root),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("fabricated_claim_id", result["reason"])

    def test_cost_appended_to_cost_jsonl(self) -> None:
        text = _scaffold_json(
            opener_story_id=self.story_item["artifact_id"],
            claim_id=self.claim_item["artifact_id"],
        )
        gen = KeynoteGenerator(api_caller=lambda _p: (text, 750, 350))
        gen.generate(
            self.run_id, self.bundle, "policy",
            {"entries": []}, str(self.repo_root),
        )
        cost_path = self.repo_root / "synthesis" / self.run_id / "cost.jsonl"
        self.assertTrue(cost_path.is_file())
        rows = [
            json.loads(line)
            for line in cost_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["call_purpose"], "keynote_arc")

    def test_keynote_md_projection_written(self) -> None:
        text = _scaffold_json(
            opener_story_id=self.story_item["artifact_id"],
            claim_id=self.claim_item["artifact_id"],
        )
        gen = KeynoteGenerator(api_caller=lambda _p: (text, 100, 50))
        gen.generate(
            self.run_id, self.bundle, "policy",
            {"entries": []}, str(self.repo_root),
        )
        md_path = (
            self.repo_root / "synthesis" / self.run_id
            / "markdown" / "keynote.md"
        )
        self.assertTrue(md_path.is_file())
        self.assertIn("VIEW ONLY", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
