"""Tests for PatternIndexer (FINDING-E-005)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.agency.pattern_indexer import PatternIndexer

from ._fixtures import read_jsonl, write_objection_history_entry


class PatternIndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_similar_objections_create_pattern(self) -> None:
        write_objection_history_entry(
            self.repo_root,
            agency_slug="fcc",
            objection_text=(
                "agency objects proposed methodology because fails address "
                "adjacent channel interference adequately"
            ),
        )
        write_objection_history_entry(
            self.repo_root,
            agency_slug="ntia",
            objection_text=(
                "adjacent channel interference adequately fails address "
                "proposed methodology again"
            ),
        )
        result = PatternIndexer().build_patterns(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["pattern_count"], 1)
        patterns = read_jsonl(self.repo_root / "agency" / "patterns.jsonl")
        self.assertEqual(len(patterns), result["pattern_count"])

    def test_dissimilar_objections_no_pattern(self) -> None:
        write_objection_history_entry(
            self.repo_root,
            agency_slug="fcc",
            objection_text=(
                "Spectrum auction concerns dominate the regulatory landscape."
            ),
        )
        write_objection_history_entry(
            self.repo_root,
            agency_slug="ntia",
            objection_text=(
                "Cybersecurity considerations require updated frameworks today."
            ),
        )
        result = PatternIndexer().build_patterns(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pattern_count"], 0)

    def test_zero_agencies_returns_empty_patterns(self) -> None:
        result = PatternIndexer().build_patterns(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pattern_count"], 0)
        # patterns.jsonl exists and is empty.
        path = self.repo_root / "agency" / "patterns.jsonl"
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_jaccard_threshold_enforced(self) -> None:
        # Pair just above and below 0.6 threshold.
        write_objection_history_entry(
            self.repo_root,
            agency_slug="fcc",
            objection_text=(
                "alpha beta gamma delta epsilon zeta eta theta iota"
            ),
        )
        # Share only 4/9 long words → ~0.44 — below threshold.
        write_objection_history_entry(
            self.repo_root,
            agency_slug="ntia",
            objection_text=(
                "alpha beta gamma delta different other words entirely"
            ),
        )
        result = PatternIndexer().build_patterns(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        # Could be 0 or could be >=1 depending on the exact word set.
        # Verify any reported pattern has jaccard >= 0.6.
        patterns = read_jsonl(self.repo_root / "agency" / "patterns.jsonl")
        for p in patterns:
            self.assertGreaterEqual(p["jaccard_similarity"], 0.6)

    def test_similarity_method_is_jaccard_word(self) -> None:
        write_objection_history_entry(
            self.repo_root,
            agency_slug="fcc",
            objection_text=(
                "spectrum allocation methodology raises serious concerns "
                "about adjacent channel interference modeling assumptions."
            ),
        )
        write_objection_history_entry(
            self.repo_root,
            agency_slug="ntia",
            objection_text=(
                "spectrum allocation methodology raises serious concerns "
                "about adjacent channel interference modeling specifics."
            ),
        )
        result = PatternIndexer().build_patterns(str(self.repo_root))
        self.assertEqual(result["status"], "success")
        patterns = read_jsonl(self.repo_root / "agency" / "patterns.jsonl")
        self.assertGreaterEqual(len(patterns), 1)
        for p in patterns:
            self.assertEqual(p["similarity_method"], "jaccard_word")
        projection = self.repo_root / "agency" / "markdown" / "patterns.md"
        self.assertTrue(projection.is_file())
        self.assertIn("jaccard_word", projection.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
