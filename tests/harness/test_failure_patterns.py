"""Tests for FailurePatternIndex (Phase G — FINDING-G-002, G-003)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.harness import FailurePatternIndex

from ._fixtures import make_failure, read_jsonl


class FailurePatternIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.index = FailurePatternIndex()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patterns(self) -> list[dict]:
        return read_jsonl(
            self.repo_root / "harness" / "failures" / "patterns.jsonl"
        )

    def _candidates(self) -> list[dict]:
        return read_jsonl(
            self.repo_root / "harness" / "failures" / "eval_candidates.jsonl"
        )

    def test_single_failure_no_pattern_created(self) -> None:
        self.index.ingest_failures(
            "run-1",
            [make_failure(detail="missing inline citation evidence section ground")],
            str(self.repo_root),
        )
        self.assertEqual(self._patterns(), [])
        pending_path = (
            self.repo_root / "harness" / "failures" / "pending_failures.jsonl"
        )
        self.assertTrue(pending_path.is_file())
        self.assertEqual(len(read_jsonl(pending_path)), 1)

    def test_two_similar_failures_create_one_pattern(self) -> None:
        detail = (
            "section context missing inline citation evidence ground retrieval recipe"
        )
        self.index.ingest_failures(
            "run-1", [make_failure(detail=detail)], str(self.repo_root)
        )
        self.index.ingest_failures(
            "run-2", [make_failure(detail=detail + " slight extra")], str(self.repo_root)
        )
        patterns = self._patterns()
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["occurrence_count"], 2)
        self.assertEqual(set(patterns[0]["member_run_ids"]), {"run-1", "run-2"})
        self.assertEqual(patterns[0]["cluster_method"], "reason_code_then_jaccard")
        self.assertEqual(patterns[0]["jaccard_threshold"], 0.7)

    def test_two_dissimilar_failures_create_two_patterns(self) -> None:
        # Same reason_code, very different details (low Jaccard).
        self.index.ingest_failures(
            "run-1",
            [make_failure(detail="alpha bravo charlie delta echo foxtrot golf")],
            str(self.repo_root),
        )
        self.index.ingest_failures(
            "run-2",
            [make_failure(detail="zulu yankee xray whiskey victor uniform tango")],
            str(self.repo_root),
        )
        # Add one more of each to clear pending → patterns.
        self.index.ingest_failures(
            "run-3",
            [make_failure(detail="alpha bravo charlie delta echo foxtrot golf")],
            str(self.repo_root),
        )
        self.index.ingest_failures(
            "run-4",
            [make_failure(detail="zulu yankee xray whiskey victor uniform tango")],
            str(self.repo_root),
        )
        patterns = self._patterns()
        self.assertEqual(len(patterns), 2)

    def test_propose_candidate_not_in_contracts_evals(self) -> None:
        # Build pattern with occurrence_count >= 3.
        for i in range(4):
            self.index.ingest_failures(
                f"run-{i}",
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.repo_root),
            )
        patterns = self._patterns()
        self.assertGreaterEqual(patterns[0]["occurrence_count"], 3)
        before_files = list(
            (self.repo_root / "contracts" / "evals").glob("*.json")
            if (self.repo_root / "contracts" / "evals").is_dir()
            else []
        )
        self.index.propose_eval_candidate(patterns[0], str(self.repo_root))
        after_files = list(
            (self.repo_root / "contracts" / "evals").glob("*.json")
            if (self.repo_root / "contracts" / "evals").is_dir()
            else []
        )
        self.assertEqual(before_files, after_files)
        # Candidate WAS written.
        cands = self._candidates()
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["requires_human_promotion"])
        self.assertEqual(cands[0]["proposed_by"], "harness_memory")
        self.assertEqual(cands[0]["status"], "candidate")

    def test_propose_candidate_skipped_if_already_exists(self) -> None:
        for i in range(4):
            self.index.ingest_failures(
                f"run-{i}",
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.repo_root),
            )
        patterns = self._patterns()
        first = self.index.propose_eval_candidate(patterns[0], str(self.repo_root))
        self.assertEqual(first["status"], "success")
        # Re-fetch the (now-mutated) pattern from disk to get eval_candidate_id.
        patterns2 = self._patterns()
        second = self.index.propose_eval_candidate(patterns2[0], str(self.repo_root))
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(len(self._candidates()), 1)

    def test_pattern_cluster_method_is_correct_const(self) -> None:
        for run in ("a", "b"):
            self.index.ingest_failures(
                run,
                [make_failure(detail="section context missing inline citation evidence")],
                str(self.repo_root),
            )
        pattern = self._patterns()[0]
        self.assertEqual(pattern["cluster_method"], "reason_code_then_jaccard")
        self.assertEqual(pattern["jaccard_threshold"], 0.7)


if __name__ == "__main__":
    unittest.main()
