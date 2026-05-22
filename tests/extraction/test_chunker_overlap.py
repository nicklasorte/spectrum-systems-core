"""Phase 2.B: ``CHUNK_OVERLAP_TURNS`` on the cascade chunker.

The cascade chunker's speaker-turn path now reads
``CHUNK_OVERLAP_TURNS`` and prepends prior turns' text onto each
chunk. The character-count fallback path is UNCHANGED (it has its
own ``OVERLAP=1`` unit-level overlap, which is unrelated to the
Phase-2.B turn-level overlap).

Default-off (``CHUNK_OVERLAP_TURNS=0`` or unset) preserves
byte-identical pre-Phase-2.B output.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker

from ._fixtures import write_text_units


class CascadeOverlapTests(unittest.TestCase):
    """Cascade chunker speaker-turn overlap (Phase 2.B)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        # The cascade chunker runs a Phase R.0 merge pass that
        # absorbs sub-MIN_CHUNK_CHARS chunks. The toy transcripts
        # below would be collapsed into a single chunk by that pass,
        # which would make the overlap effect invisible. Disable
        # merge so the speaker-turn structure stays intact.
        self._prev_merge = os.environ.get("CHUNK_MERGE_ENABLED")
        os.environ["CHUNK_MERGE_ENABLED"] = "false"
        # Tests start with CHUNK_OVERLAP_TURNS unset (default off).
        self._prev_overlap = os.environ.pop("CHUNK_OVERLAP_TURNS", None)

    def tearDown(self) -> None:
        if self._prev_merge is None:
            os.environ.pop("CHUNK_MERGE_ENABLED", None)
        else:
            os.environ["CHUNK_MERGE_ENABLED"] = self._prev_merge
        if self._prev_overlap is None:
            os.environ.pop("CHUNK_OVERLAP_TURNS", None)
        else:
            os.environ["CHUNK_OVERLAP_TURNS"] = self._prev_overlap
        self._tmp.cleanup()

    def _three_turn_transcript(self, source_id: str) -> None:
        texts = [
            "Alice Smith   1:00",
            "alpha one alpha two alpha three alpha four alpha five",
            "Bob Jones   1:05",
            "bravo one bravo two bravo three bravo four bravo five",
            "Carol Ramirez   1:10",
            "charlie one charlie two charlie three charlie four charlie five",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id=source_id,
            texts=texts,
        )

    # ---- default-off byte-identicality -------------------------------

    def test_default_off_no_overlap_metadata(self) -> None:
        self._three_turn_transcript("m-default-off")
        result = Chunker().chunk(
            "m-default-off", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        for c in result["chunks"]:
            self.assertNotIn("overlap_turns_prepended", c)
            self.assertNotIn("overlap_clamped", c)
            self.assertNotIn("prepended_overlap_chunk_ids", c)

    # ---- overlap=1 ---------------------------------------------------

    def test_overlap_one_prepends_one_prior_turn(self) -> None:
        os.environ["CHUNK_OVERLAP_TURNS"] = "1"
        self._three_turn_transcript("m-overlap-1")
        result = Chunker().chunk("m-overlap-1", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 3)

        # Chunk 0: no predecessor.
        self.assertEqual(chunks[0]["overlap_turns_prepended"], 0)
        self.assertEqual(chunks[0]["prepended_overlap_chunk_ids"], [])
        # Chunk 0's text is unchanged — only Alice's content.
        self.assertIn("alpha one", chunks[0]["text"])
        self.assertNotIn("bravo one", chunks[0]["text"])

        # Chunk 1: one prior turn (Alice) prepended.
        self.assertEqual(chunks[1]["overlap_turns_prepended"], 1)
        # The prepended_overlap_chunk_ids field carries Alice's UUID.
        self.assertEqual(len(chunks[1]["prepended_overlap_chunk_ids"]), 1)
        self.assertEqual(
            chunks[1]["prepended_overlap_chunk_ids"][0],
            chunks[0]["chunk_id"],
        )
        # Bob's text now starts with Alice's content.
        self.assertIn("alpha one", chunks[1]["text"])
        self.assertIn("bravo one", chunks[1]["text"])
        # char_count is recomputed.
        self.assertEqual(chunks[1]["char_count"], len(chunks[1]["text"]))

    # ---- overlap=2 ---------------------------------------------------

    def test_overlap_two_prepends_up_to_two_prior_turns(self) -> None:
        os.environ["CHUNK_OVERLAP_TURNS"] = "2"
        self._three_turn_transcript("m-overlap-2")
        result = Chunker().chunk("m-overlap-2", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["overlap_turns_prepended"], 0)
        self.assertEqual(chunks[1]["overlap_turns_prepended"], 1)
        self.assertEqual(chunks[2]["overlap_turns_prepended"], 2)
        self.assertEqual(
            chunks[2]["prepended_overlap_chunk_ids"],
            [chunks[0]["chunk_id"], chunks[1]["chunk_id"]],
        )
        # All three turn texts appear in chunk 2.
        self.assertIn("alpha one", chunks[2]["text"])
        self.assertIn("bravo one", chunks[2]["text"])
        self.assertIn("charlie one", chunks[2]["text"])

    # ---- compound prevention -----------------------------------------

    def test_overlap_does_not_compound(self) -> None:
        """Each chunk's prepend uses the prior chunks' ORIGINAL text,
        not their overlap-augmented text. Without this guard the
        prepended-text would grow quadratically.
        """
        os.environ["CHUNK_OVERLAP_TURNS"] = "1"
        self._three_turn_transcript("m-no-compound")
        result = Chunker().chunk("m-no-compound", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        # Chunk 2 (Carol) should contain Bob's text exactly once,
        # not Bob+Alice (which would happen if chunk 2 prepended
        # chunk 1's already-overlap-augmented text).
        self.assertEqual(chunks[2]["text"].count("bravo one"), 1)
        self.assertEqual(chunks[2]["text"].count("alpha one"), 0)

    # ---- text_hash recomputed on overlap -----------------------------

    def test_text_hash_consistent_with_overlapped_text(self) -> None:
        """The chunk envelope requires text_hash == sha256(text). The
        overlap pass must recompute it after modifying the text or
        the schema validator will reject the chunk.
        """
        os.environ["CHUNK_OVERLAP_TURNS"] = "1"
        self._three_turn_transcript("m-hash-consistent")
        result = Chunker().chunk(
            "m-hash-consistent", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        import hashlib
        for c in result["chunks"]:
            expected = "sha256:" + hashlib.sha256(
                c["text"].encode("utf-8")
            ).hexdigest()
            self.assertEqual(c["text_hash"], expected)


if __name__ == "__main__":
    unittest.main()
