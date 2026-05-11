"""Tests for GlossaryManager.

Phase M2. Verifies: index-once invariant, retrieval correctness, cap at
5 terms, regulatory verb inclusion, the READ-ONLY instruction, and empty
input handling.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction.glossary_manager import GlossaryManager


def _write_term(
    dir_path: Path, term: str, definition: str, *,
    source: str = "ITU",
    is_verb: bool = False,
    verb_def: str | None = None,
) -> None:
    artifact = {
        "glossary_term_id": "11111111-1111-1111-1111-" + ("0" * 12),
        "term": term,
        "definition": definition,
        "authoritative_source": source,
        "related_terms": [],
        "is_regulatory_verb": is_verb,
        "canonical_verb_definition": verb_def,
        "artifact_type": "glossary_term",
        "schema_version": "1.0.0",
        "created_at": "1970-01-01T00:00:00+00:00",
        "provenance": {"produced_by": "GlossaryManager"},
    }
    slug = term.lower().replace(" ", "_").replace("/", "_")
    (dir_path / f"{slug}.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class GlossaryManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.glossary_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_basic(self) -> GlossaryManager:
        _write_term(self.glossary_dir, "FSS", "Fixed Satellite Service")
        _write_term(self.glossary_dir, "ITU", "Intl Telecommunication Union")
        _write_term(self.glossary_dir, "FCC", "Federal Communications Commission")
        return GlossaryManager(str(self.glossary_dir))

    def test_index_built_once_not_per_chunk(self) -> None:
        gm = self._seed_basic()
        self.assertEqual(gm.index_built_count, 1)
        for _ in range(5):
            gm.retrieve_for_chunk("FSS protection zone ITU criteria")
        self.assertEqual(
            gm.index_built_count, 1,
            "Index must be built exactly once across many retrievals.",
        )

    def test_retrieves_correct_terms_for_chunk(self) -> None:
        gm = self._seed_basic()
        results = gm.retrieve_for_chunk(
            "FSS protection zone analysis using ITU criteria"
        )
        # Content assertion: actual terms must be FSS and ITU.
        terms = {r["term"] for r in results}
        self.assertIn("FSS", terms)
        self.assertIn("ITU", terms)
        # FCC was not in the chunk text, should not appear.
        self.assertNotIn("FCC", terms)

    def test_cap_at_five_terms(self) -> None:
        # Seed 8 distinct terms, all present in the chunk
        for n in range(8):
            _write_term(self.glossary_dir, f"TERM{n}", f"definition {n}")
        gm = GlossaryManager(str(self.glossary_dir))
        chunk = " ".join(f"TERM{n}" for n in range(8))
        results = gm.retrieve_for_chunk(chunk)
        self.assertEqual(len(results), 5)

    def test_regulatory_verb_in_format(self) -> None:
        _write_term(
            self.glossary_dir, "approved",
            "Group reached explicit agreement.",
            source="CLAUDE.md", is_verb=True,
            verb_def="Explicit affirmative decision; no recorded objections.",
        )
        gm = GlossaryManager(str(self.glossary_dir))
        terms = gm.retrieve_for_chunk("the proposal was approved by the WG")
        block = gm.format_for_prompt(terms)
        self.assertIn("approved", block)
        # The canonical verb definition is the regulatory-verb metadata
        self.assertIn(
            "Explicit affirmative decision; no recorded objections.",
            block,
        )

    def test_read_only_instruction_in_formatted_block(self) -> None:
        gm = self._seed_basic()
        terms = gm.retrieve_for_chunk("FSS reading")
        block = gm.format_for_prompt(terms)
        self.assertIn("read-only", block.lower())
        self.assertIn("do not include in output", block.lower())

    def test_empty_chunk_returns_empty_list(self) -> None:
        gm = self._seed_basic()
        self.assertEqual(gm.retrieve_for_chunk(""), [])
        self.assertEqual(gm.retrieve_for_chunk("   "), [])

    def test_empty_terms_format_returns_empty_string(self) -> None:
        gm = GlossaryManager(str(self.glossary_dir))
        self.assertEqual(gm.format_for_prompt([]), "")

    def test_missing_glossary_dir_does_not_raise(self) -> None:
        # Construct against a path that does not exist.
        gm = GlossaryManager("/nonexistent/path/should/be/fine")
        # Indexing still happens (over an empty set).
        self.assertTrue(gm.is_indexed)
        self.assertEqual(gm.term_count, 0)
        self.assertEqual(gm.retrieve_for_chunk("anything"), [])

    def test_longer_terms_rank_first(self) -> None:
        _write_term(self.glossary_dir, "FSS", "x")
        _write_term(self.glossary_dir, "FSS protection zone", "y")
        gm = GlossaryManager(str(self.glossary_dir))
        results = gm.retrieve_for_chunk("FSS protection zone is critical")
        self.assertEqual(results[0]["term"], "FSS protection zone")


if __name__ == "__main__":
    unittest.main()
