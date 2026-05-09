"""Tests for SynthesisReviewGateway (FINDING-F-006: shared review pattern)."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.synthesis_review_gateway import (
    SynthesisReviewGateway,
)


def _write_review_form_with(
    *,
    path: Path,
    review_status: str = "pending",
    reviewer_id: str = "",
    decision: str = "",
    ingestion_at: str = "2024-01-01T00:00:00Z",
) -> None:
    body = (
        "---\n"
        f'run_id: "{path.stem.replace("_review", "")}"\n'
        'review_type: "report"\n'
        'audience: "technical"\n'
        'purpose: "report"\n'
        f'reviewer_id: "{reviewer_id}"\n'
        f'decision: "{decision}"\n'
        'reviewed_at: ""\n'
        'notes: ""\n'
        f"review_status: {review_status}\n"
        f'ingestion_at: "{ingestion_at}"\n'
        "---\n\n"
        "# Synthesis Review\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class SynthesisReviewGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.vault_root = self.repo_root / "vault"
        self.run_id = str(uuid.uuid4())
        # Place a draft and scaffold for the gateway to operate on.
        run_dir = self.repo_root / "synthesis" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report = {"draft_id": str(uuid.uuid4()), "status": "draft"}
        scaffold = {"scaffold_id": str(uuid.uuid4()), "status": "draft"}
        (run_dir / "report_draft.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        (run_dir / "keynote_scaffold.json").write_text(
            json.dumps(scaffold, indent=2, sort_keys=True), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_review_form_written_to_correct_path(self) -> None:
        path = SynthesisReviewGateway().emit_review_form(
            run_id=self.run_id,
            audience="technical",
            purpose="report",
            report_draft={"draft_id": "x", "sections": [], "status": "draft", "bundle_hash": ""},
            keynote_scaffold=None,
            cost_total=0.01,
            vault_root=str(self.vault_root),
            repo_root=str(self.repo_root),
        )
        expected = (
            self.vault_root / "Reviews" / "Synthesis" / "Pending"
            / f"{self.run_id}_review.md"
        )
        self.assertEqual(Path(path), expected)
        self.assertTrue(expected.is_file())

    def test_pending_returns_awaiting(self) -> None:
        form_path = (
            self.vault_root / "Reviews" / "Synthesis" / "Pending"
            / f"{self.run_id}_review.md"
        )
        recent = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_review_form_with(path=form_path, ingestion_at=recent)
        result = SynthesisReviewGateway().poll_for_completion(
            run_id=self.run_id,
            vault_root=str(self.vault_root),
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "awaiting")

    def test_approved_sets_status(self) -> None:
        form_path = (
            self.vault_root / "Reviews" / "Synthesis" / "Pending"
            / f"{self.run_id}_review.md"
        )
        _write_review_form_with(
            path=form_path,
            review_status="submitted",
            reviewer_id="reviewer-1",
            decision="approve",
        )
        result = SynthesisReviewGateway().poll_for_completion(
            run_id=self.run_id,
            vault_root=str(self.vault_root),
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["decision"], "approve")
        report = json.loads(
            (
                self.repo_root / "synthesis" / self.run_id / "report_draft.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report["status"], "approved")

    def test_timeout_blocks_gracefully(self) -> None:
        form_path = (
            self.vault_root / "Reviews" / "Synthesis" / "Pending"
            / f"{self.run_id}_review.md"
        )
        _write_review_form_with(
            path=form_path,
            review_status="pending",
            ingestion_at="2024-01-01T00:00:00Z",
        )

        def _now() -> datetime.datetime:
            return datetime.datetime(2024, 12, 31, 0, 0, 0)

        result = SynthesisReviewGateway().poll_for_completion(
            run_id=self.run_id,
            vault_root=str(self.vault_root),
            repo_root=str(self.repo_root),
            timeout_hours=1,
            now=_now,
        )
        self.assertEqual(result["status"], "timeout")
        report = json.loads(
            (
                self.repo_root / "synthesis" / self.run_id / "report_draft.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report["status"], "rejected")

    def test_vault_dir_created_if_absent(self) -> None:
        # Even if the vault dir does not yet exist, emit_review_form makes it.
        new_vault = self.repo_root / "fresh_vault"
        self.assertFalse(new_vault.exists())
        SynthesisReviewGateway().emit_review_form(
            run_id=self.run_id,
            audience="technical",
            purpose="report",
            report_draft=None,
            keynote_scaffold=None,
            cost_total=0.0,
            vault_root=str(new_vault),
            repo_root=str(self.repo_root),
        )
        self.assertTrue(
            (new_vault / "Reviews" / "Synthesis" / "Pending").is_dir()
        )


if __name__ == "__main__":
    unittest.main()
