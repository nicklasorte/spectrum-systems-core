"""Tests for EntropyAuditor (Phase G — FINDING-G-007)."""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.harness import (
    EntropyAuditor,
    EvalScoreHistory,
    FailurePatternIndex,
    OutcomeMemoryStore,
    OverrideStore,
)

from ._fixtures import make_failure


class EntropyAuditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.audit = EntropyAuditor()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self) -> set[Path]:
        return set(p for p in self.repo_root.rglob("*") if p.is_file())

    def test_audit_runs_without_crash_on_empty_system(self) -> None:
        result = self.audit.run_audit(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_flagged"], 0)

    def test_eval_always_passing_flagged_low(self) -> None:
        # Create an eval case file.
        contracts = self.repo_root / "contracts" / "evals"
        contracts.mkdir(parents=True, exist_ok=True)
        (contracts / "report_draft_evals.json").write_text(
            json.dumps([{"name": "EVAL-1", "metric_name": "x"}]) + "\n",
            encoding="utf-8",
        )
        history = EvalScoreHistory()
        for _ in range(35):
            history.record_eval_results(
                "run-x",
                [{"name": "always_pass", "status": "pass"}],
                "report_draft",
                str(self.repo_root),
            )
        result = self.audit.run_audit(str(self.repo_root))
        flagged = result["report"]["flagged_items"]
        eval_flags = [f for f in flagged if f["item_type"] == "eval_case"]
        self.assertGreaterEqual(len(eval_flags), 1)
        self.assertEqual(eval_flags[0]["severity"], "low")

    def test_pattern_no_candidate_flagged_medium(self) -> None:
        index = FailurePatternIndex()
        for i in range(4):
            index.ingest_failures(
                f"run-{i}",
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.repo_root),
            )
        result = self.audit.run_audit(str(self.repo_root))
        flagged = result["report"]["flagged_items"]
        pat_flags = [f for f in flagged if f["item_type"] == "failure_pattern"]
        self.assertGreaterEqual(len(pat_flags), 1)
        self.assertEqual(pat_flags[0]["severity"], "medium")
        self.assertIn("propose-eval-candidate", pat_flags[0]["recommended_action"])

    def test_override_expiring_soon_flagged_high(self) -> None:
        OverrideStore().record_override(
            decision_context="bypass section grounding for paper-A keynote",
            overridden_artifact_id=str(uuid.uuid4()),
            overridden_eval_or_block="grounding_eval",
            rationale=(
                "human-verified that the citations exist in the supplementary "
                "materials archive — temporary bypass"
            ),
            overriding_human_id="reviewer-1",
            repo_root=str(self.repo_root),
            expires_days=15,
        )
        result = self.audit.run_audit(str(self.repo_root))
        flagged = result["report"]["flagged_items"]
        ovr_flags = [f for f in flagged if f["item_type"] == "override"]
        self.assertGreaterEqual(len(ovr_flags), 1)
        self.assertEqual(ovr_flags[0]["severity"], "high")

    def test_effectiveness_below_threshold_flagged(self) -> None:
        store = OutcomeMemoryStore()
        for _ in range(3):
            store.record_revision_outcome(
                {
                    "diff_id": str(uuid.uuid4()),
                    "instruction_id": "i",
                    "paper_source_id": "p",
                    "status": "failure",
                },
                {
                    "instruction_id": "i",
                    "issue_type": "scope",
                    "instruction_text": "tighten boundaries clearly defined territory",
                    "priority": "high",
                },
                str(self.repo_root),
            )
        result = self.audit.run_audit(str(self.repo_root))
        flagged = result["report"]["flagged_items"]
        eff_flags = [
            f for f in flagged if f["item_type"] == "outcome_effectiveness"
        ]
        self.assertGreaterEqual(len(eff_flags), 1)
        self.assertEqual(eff_flags[0]["severity"], "medium")

    def test_report_schema_valid(self) -> None:
        result = self.audit.run_audit(str(self.repo_root))
        self.assertEqual(result["status"], "success")

    def test_report_appended_not_overwritten(self) -> None:
        self.audit.run_audit(str(self.repo_root))
        self.audit.run_audit(str(self.repo_root))
        path = self.repo_root / "harness" / "entropy" / "reports.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_projection_written_with_view_only(self) -> None:
        result = self.audit.run_audit(str(self.repo_root))
        path = self.repo_root / "harness" / "markdown" / "entropy.md"
        self.assertTrue(path.is_file())
        body = path.read_text(encoding="utf-8")
        self.assertIn("VIEW ONLY", body)

    def test_no_auto_deletion_by_audit(self) -> None:
        # Seed a few files in harness/.
        (self.repo_root / "harness" / "outcomes").mkdir(parents=True, exist_ok=True)
        memory = self.repo_root / "harness" / "outcomes" / "memory.jsonl"
        memory.write_text("", encoding="utf-8")
        before = self._snapshot()
        self.audit.run_audit(str(self.repo_root))
        after = self._snapshot()
        # All before files must remain after the audit (additions are allowed).
        self.assertTrue(before.issubset(after))


if __name__ == "__main__":
    unittest.main()
