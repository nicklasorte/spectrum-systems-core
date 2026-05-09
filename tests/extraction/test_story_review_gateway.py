"""Tests for StoryReviewGateway (Phase C, Step 10 + Red Team #3)."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict

from spectrum_systems_core.extraction import StoryReviewGateway


def _make_candidate(story_id: str, source_id: str) -> Dict[str, Any]:
    return {
        "story_id": story_id,
        "source_id": source_id,
        "source_family": "notes",
        "chunk_id": str(uuid.uuid4()),
        "unit_ids": [str(uuid.uuid4())],
        "page_numbers": [],
        "source_excerpt": "An excerpt that is long enough to pass the eval.",
        "story_summary": "A short summary that is at least twenty chars.",
        "possible_theme": "trial themes",
        "tier_guess": "tier_1",
        "why_it_might_work": "It carries a five-second moment.",
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
        "status": "candidate",
        "superseded_by": None,
        "created_at": "2026-05-09T00:00:00+00:00",
        "provenance": {
            "produced_by": {"component": "test", "version": "1.0.0"},
            "input_artifact_ids": [],
            "execution_fingerprint_hash": "sha256:" + ("0" * 64),
        },
    }


def _write_candidate(repo_root: Path, source_id: str, candidate: Dict[str, Any]) -> Path:
    target = repo_root / "processed" / "notes" / source_id / "stories"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "candidates.jsonl"
    with path.open("w") as fh:
        fh.write(json.dumps(candidate) + "\n")
    return path


class StoryReviewGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.vault_root = self.repo_root / "vault"
        self.vault_root.mkdir(parents=True)
        self.story_id = str(uuid.uuid4())
        self.source_id = "src-review-001"
        self.candidate = _make_candidate(self.story_id, self.source_id)
        _write_candidate(self.repo_root, self.source_id, self.candidate)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_form_written_to_correct_path(self) -> None:
        gateway = StoryReviewGateway()
        path = gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root)
        )
        expected = (
            self.vault_root / "Reviews" / "Stories" / "Pending"
            / f"{self.story_id}_review.md"
        )
        self.assertEqual(Path(path), expected)
        self.assertTrue(expected.is_file())

    def test_pending_review_returns_awaiting(self) -> None:
        gateway = StoryReviewGateway()
        gateway.emit_review_form(self.story_id, self.candidate, str(self.vault_root))
        result = gateway.poll_and_promote(
            self.story_id, self.source_id,
            str(self.vault_root), str(self.repo_root)
        )
        self.assertEqual(result["status"], "awaiting")

    def _submit(self, *, decision: str, reviewer_id: str = "alice") -> None:
        path = (
            self.vault_root / "Reviews" / "Stories" / "Pending"
            / f"{self.story_id}_review.md"
        )
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            'review_status: pending', "review_status: submitted"
        )
        text = text.replace(
            'reviewer_id: ""', f'reviewer_id: "{reviewer_id}"'
        )
        text = text.replace('decision: ""', f'decision: "{decision}"')
        path.write_text(text, encoding="utf-8")

    def test_submitted_approve_promotes_story(self) -> None:
        gateway = StoryReviewGateway()
        gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root)
        )
        self._submit(decision="approve")
        result = gateway.poll_and_promote(
            self.story_id, self.source_id,
            str(self.vault_root), str(self.repo_root)
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["decision"], "approve")
        promoted_path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "promoted" / f"{self.story_id}.json"
        )
        self.assertTrue(promoted_path.is_file())
        promoted = json.loads(promoted_path.read_text(encoding="utf-8"))
        self.assertEqual(promoted["status"], "promoted")

    def test_submitted_reject_blocks_story(self) -> None:
        gateway = StoryReviewGateway()
        gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root)
        )
        self._submit(decision="reject")
        gateway.poll_and_promote(
            self.story_id, self.source_id,
            str(self.vault_root), str(self.repo_root)
        )
        candidates_path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "candidates.jsonl"
        )
        records = [
            json.loads(line)
            for line in candidates_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(records[0]["status"], "blocked")
        self.assertIn("reviewer_rejected", records[0]["block_reason"])

    def test_blank_reviewer_id_blocks_promotion(self) -> None:
        gateway = StoryReviewGateway()
        gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root)
        )
        self._submit(decision="approve", reviewer_id="")
        result = gateway.poll_and_promote(
            self.story_id, self.source_id,
            str(self.vault_root), str(self.repo_root)
        )
        self.assertEqual(result["status"], "awaiting")
        self.assertIn("blank_reviewer_id", result["reason"])

    def test_timeout_blocks_not_crashes(self) -> None:
        gateway = StoryReviewGateway()
        gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root),
            now=lambda: datetime.datetime(2026, 1, 1, 0, 0, 0),
        )
        # Now jump forward >72 hours.
        result = gateway.poll_and_promote(
            self.story_id, self.source_id,
            str(self.vault_root), str(self.repo_root),
            now=lambda: datetime.datetime(2026, 1, 5, 0, 0, 0),
        )
        self.assertEqual(result["status"], "timeout")
        # Story not promoted.
        promoted_dir = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "promoted"
        )
        self.assertFalse(any(promoted_dir.glob("*.json")) if promoted_dir.exists() else False)
        # Candidate marked blocked with review_timeout.
        candidates_path = (
            self.repo_root / "processed" / "notes" / self.source_id
            / "stories" / "candidates.jsonl"
        )
        records = [
            json.loads(line)
            for line in candidates_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(records[0]["status"], "blocked")
        self.assertIn("review_timeout", records[0]["block_reason"])

    def test_pending_dir_created_if_absent(self) -> None:
        # Vault root exists but Reviews/Stories/Pending does not.
        gateway = StoryReviewGateway()
        path = gateway.emit_review_form(
            self.story_id, self.candidate, str(self.vault_root)
        )
        self.assertTrue(Path(path).is_file())


if __name__ == "__main__":
    unittest.main()
