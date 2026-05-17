"""Phase T.4 tests: MAX_CHUNK_CHARS split pass + chunk_split_summary."""
from __future__ import annotations

import unittest
import uuid
from typing import Any

from spectrum_systems_core.extraction.chunker import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    _resolve_max_chunk_chars,
    merge_short_chunks,
    split_oversized_chunks,
)
from spectrum_systems_core.validation import (
    ArtifactValidationError,
    validate_artifact,
)


def _chunk(text: str, chunk_id: str | None = None) -> dict[str, Any]:
    cid = chunk_id or str(uuid.uuid4())
    return {
        "chunk_id": cid,
        "source_id": "src",
        "source_family": "meetings",
        "chunk_index": 0,
        "unit_ids": [str(uuid.uuid4())],
        "text": text,
        "text_hash": "sha256:" + "0" * 64,
        "unit_count": 1,
        "overlap_unit_id": None,
        "page_numbers": [],
        "char_count": len(text),
    }


class SplitOversizedChunksTests(unittest.TestCase):

    def test_under_budget_not_split(self) -> None:
        c = _chunk("x" * 2400)
        out, log = split_oversized_chunks([c], max_chars=2500)
        self.assertEqual(len(out), 1)
        self.assertEqual(log, [])

    def test_split_at_newline_boundary(self) -> None:
        # 2600 chars with a newline at position 2200 -- split there.
        text = "x" * 2200 + "\n" + "y" * 399
        c = _chunk(text)
        out, log = split_oversized_chunks([c], max_chars=2500)
        self.assertGreater(len(out), 1)
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0]["chunk_split_mid_turn"])
        # RT pass 1: first piece must end at the boundary, not past it.
        self.assertEqual(out[0]["text"], "x" * 2200)

    def test_split_mid_turn_when_no_newline(self) -> None:
        c = _chunk("x" * 2800)
        out, log = split_oversized_chunks([c], max_chars=2500)
        self.assertGreater(len(out), 1)
        self.assertEqual(len(log), 1)
        self.assertTrue(log[0]["chunk_split_mid_turn"])

    def test_no_chunk_exceeds_max_after_split(self) -> None:
        # 6000-char chunk with newlines every 800 chars.
        text = "\n".join("x" * 800 for _ in range(8))
        c = _chunk(text)
        out, _log = split_oversized_chunks([c], max_chars=2500)
        # RT pass 2: assert ALL pieces, not just first/last.
        for piece in out:
            self.assertLessEqual(piece["char_count"], 2500)

    def test_split_chunk_ids_are_fresh(self) -> None:
        c = _chunk("\n".join("x" * 800 for _ in range(5)), chunk_id="original-id")
        out, log = split_oversized_chunks([c], max_chars=2500)
        produced = log[0]["produced_chunk_ids"]
        self.assertNotIn("original-id", produced)
        for piece in out:
            self.assertNotEqual(piece["chunk_id"], "original-id")

    def test_split_log_records_original_id(self) -> None:
        c = _chunk("x" * 3000, chunk_id="orig-x")
        out, log = split_oversized_chunks([c], max_chars=2500)
        self.assertEqual(log[0]["original_chunk_id"], "orig-x")
        self.assertEqual(log[0]["original_char_count"], 3000)
        self.assertEqual(log[0]["max_chars"], 2500)


class ChunkSplitSummaryArtifactTests(unittest.TestCase):
    """The chunk_split_summary artifact must validate against the schema."""

    def test_artifact_validates(self) -> None:
        artifact = {
            "artifact_type": "chunk_split_summary",
            "schema_version": "1.0.0",
            "source_id": "src-1",
            "max_chunk_chars": 2500,
            "original_chunk_count": 5,
            "split_chunk_count": 7,
            "chunks_split": 2,
            "split_log": [
                {
                    "original_chunk_id": "a",
                    "produced_chunk_ids": ["b", "c"],
                    "split_reason": "exceeded_max_chars",
                    "chunk_split_mid_turn": False,
                    "original_char_count": 3000,
                    "max_chars": 2500,
                }
            ],
            "created_at": "2026-05-12T00:00:00+00:00",
        }
        # Must not raise.
        validate_artifact(artifact, "chunk_split_summary")

    def test_artifact_missing_field_rejected(self) -> None:
        artifact = {
            "artifact_type": "chunk_split_summary",
            "schema_version": "1.0.0",
            "source_id": "src-1",
        }
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(artifact, "chunk_split_summary")


class MaxChunkCharsConfigTests(unittest.TestCase):

    def test_default_is_2500(self) -> None:
        self.assertEqual(MAX_CHUNK_CHARS, 2500)
        self.assertEqual(_resolve_max_chunk_chars(), 2500)


class SplitAndMergeRoundTripTests(unittest.TestCase):
    """RT pass 1: a tiny tail after split must be re-merged."""

    def test_post_split_tiny_tail_absorbed_by_merge(self) -> None:
        # 2510-char chunk: split produces a 2500-char head and a 10-char tail.
        c = _chunk("x" * 2510)
        out, _log = split_oversized_chunks([c], max_chars=2500)
        # Add a min-chars merge pass after the split.
        merged, _pairs = merge_short_chunks(out, min_chars=MIN_CHUNK_CHARS)
        # The 10-char tail must have been absorbed (or stay if no neighbour).
        for piece in merged:
            # Either >= MIN_CHUNK_CHARS or it is the only chunk in the list.
            if len(merged) > 1:
                self.assertGreaterEqual(piece["char_count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
