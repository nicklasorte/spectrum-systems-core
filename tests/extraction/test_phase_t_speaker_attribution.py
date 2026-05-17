"""Phase T.3 tests: deterministic speaker attribution."""
from __future__ import annotations

import unittest

from spectrum_systems_core.extraction.speaker_attribution import (
    attribute_speakers,
    resolve_speaker,
)


class ResolveSpeakerTests(unittest.TestCase):

    def test_single_speaker_chunk(self) -> None:
        chunks = {"c-1": {"chunk_id": "c-1", "speaker": "Alice", "text": "x"}}
        item = {"source_turn_ids": ["c-1"]}
        speaker, ambiguous, missing = resolve_speaker(item, chunks)
        self.assertEqual(speaker, "Alice")
        self.assertFalse(ambiguous)
        self.assertFalse(missing)

    def test_multi_speaker_chunk_first_wins_and_ambiguous_true(self) -> None:
        chunks = {
            "c-1": {
                "chunk_id": "c-1",
                "speaker": "Alice",
                "speakers": ["Alice", "Bob"],
                "text": "x",
            }
        }
        item = {"source_turn_ids": ["c-1"]}
        speaker, ambiguous, missing = resolve_speaker(item, chunks)
        self.assertEqual(speaker, "Alice")
        self.assertTrue(ambiguous)
        self.assertFalse(missing)

    def test_chunk_not_in_lookup_returns_none_and_missing_true(self) -> None:
        item = {"source_turn_ids": ["c-1"]}
        speaker, ambiguous, missing = resolve_speaker(item, {})
        self.assertIsNone(speaker)
        self.assertFalse(ambiguous)
        self.assertTrue(missing)

    def test_no_source_turns_returns_missing(self) -> None:
        speaker, ambiguous, missing = resolve_speaker({}, {})
        self.assertIsNone(speaker)
        self.assertTrue(missing)


class AttributeSpeakersTests(unittest.TestCase):

    def test_findings_only_for_missing_speakers(self) -> None:
        chunks = {
            "c-1": {"chunk_id": "c-1", "speaker": "Alice", "text": "x"},
        }
        items = [
            {"decision_text": "a", "source_turn_ids": ["c-1"]},
            {"decision_text": "b", "source_turn_ids": ["c-2"]},  # missing chunk
        ]
        annotated, findings = attribute_speakers(items, chunks)
        # RT pass 2: BOTH the speaker=None AND the finding must be
        # present for the missing case. Asserts both, not either.
        self.assertEqual(len(annotated), 2)
        self.assertEqual(annotated[0]["speaker"], "Alice")
        self.assertIsNone(annotated[1]["speaker"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_code, "speaker_attribution_missing")
        self.assertEqual(findings[0].severity, "info")

    def test_input_not_mutated(self) -> None:
        chunks = {"c-1": {"chunk_id": "c-1", "speaker": "Alice", "text": "x"}}
        item = {"decision_text": "a", "source_turn_ids": ["c-1"]}
        annotated, _ = attribute_speakers([item], chunks)
        self.assertNotIn("speaker", item)
        self.assertEqual(annotated[0]["speaker"], "Alice")

    def test_speaker_ambiguous_field_on_every_item(self) -> None:
        chunks = {"c-1": {"chunk_id": "c-1", "speaker": "Alice", "text": "x"}}
        items = [{"decision_text": "a", "source_turn_ids": ["c-1"]}]
        annotated, _ = attribute_speakers(items, chunks)
        self.assertIn("speaker_ambiguous", annotated[0])
        self.assertFalse(annotated[0]["speaker_ambiguous"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
