"""Tests for the Phase C Chunker."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker

from ._fixtures import read_jsonl, write_text_units


class ChunkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_source_produces_chunks(self) -> None:
        texts = [f"Paragraph number {i} is here." for i in range(20)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-001", texts=texts
        )
        result = Chunker().chunk("ns-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertGreater(len(result["chunks"]), 1)
        chunks_path = (
            self.repo_root / "processed" / "notes" / "ns-001" / "stories"
            / "chunks.jsonl"
        )
        self.assertTrue(chunks_path.is_file())
        loaded = read_jsonl(chunks_path)
        self.assertEqual(len(loaded), len(result["chunks"]))

    def test_overlap_unit_id_set_correctly(self) -> None:
        texts = [f"P{i}" * 10 for i in range(20)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-002", texts=texts
        )
        result = Chunker().chunk("ns-002", str(self.repo_root))
        self.assertEqual(result["status"], "success")
        chunks = result["chunks"]
        self.assertIsNone(chunks[0]["overlap_unit_id"])
        for prev, curr in zip(chunks, chunks[1:]):
            # The last unit_id of prev must equal overlap_unit_id of curr.
            self.assertEqual(prev["unit_ids"][-1], curr["overlap_unit_id"])
            # And must equal the first unit_id of curr.
            self.assertEqual(curr["unit_ids"][0], curr["overlap_unit_id"])

    def test_final_chunk_smaller_than_chunk_size_ok(self) -> None:
        # 5 units → 1 chunk of 5 (smaller than CHUNK_SIZE=8) — must succeed.
        texts = [f"Paragraph {i} text here." for i in range(5)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-003", texts=texts
        )
        result = Chunker().chunk("ns-003", str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["chunks"]), 1)
        self.assertEqual(result["chunks"][0]["unit_count"], 5)

    def test_empty_source_fails_cleanly(self) -> None:
        # No text_units.jsonl at all.
        result = Chunker().chunk("missing-id", str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertIn("text_units_not_found", result["reason"])

    def test_empty_text_units_fails(self) -> None:
        # Empty file.
        target = self.repo_root / "processed" / "notes" / "empty-id"
        target.mkdir(parents=True, exist_ok=True)
        (target / "text_units.jsonl").write_text("", encoding="utf-8")
        result = Chunker().chunk("empty-id", str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertIn("text_units_empty", result["reason"])

    def test_page_numbers_empty_array_for_txt_source(self) -> None:
        # No page_number in locator (txt source) → page_numbers is [].
        texts = [f"Paragraph {i} text." for i in range(10)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-004", texts=texts
        )
        result = Chunker().chunk("ns-004", str(self.repo_root))
        self.assertEqual(result["status"], "success")
        for chunk in result["chunks"]:
            self.assertIsInstance(chunk["page_numbers"], list)
            self.assertEqual(chunk["page_numbers"], [])

    def test_page_numbers_present_for_book_source(self) -> None:
        texts = [f"Paragraph {i}." for i in range(10)]
        pages = [1, 1, 1, 2, 2, 2, 2, 3, 3, 3]
        write_text_units(
            self.repo_root,
            family="books",
            source_id="bk-001",
            texts=texts,
            page_numbers=pages,
        )
        result = Chunker().chunk("bk-001", str(self.repo_root))
        self.assertEqual(result["status"], "success")
        first_chunk = result["chunks"][0]
        self.assertEqual(first_chunk["page_numbers"], sorted(set(pages[:8])))

    def test_chunks_jsonl_each_line_valid_json(self) -> None:
        texts = [f"Para {i}" for i in range(20)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-005", texts=texts
        )
        Chunker().chunk("ns-005", str(self.repo_root))
        path = (
            self.repo_root / "processed" / "notes" / "ns-005" / "stories"
            / "chunks.jsonl"
        )
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                parsed = json.loads(line)  # raises on invalid line
                self.assertIn("chunk_id", parsed)

    def test_chunk_hashes_stable_across_runs(self) -> None:
        texts = [f"Paragraph {i} text." for i in range(20)]
        write_text_units(
            self.repo_root, family="notes", source_id="ns-006", texts=texts
        )
        a = Chunker().chunk("ns-006", str(self.repo_root))
        b = Chunker().chunk("ns-006", str(self.repo_root))
        self.assertEqual(a["status"], "success")
        self.assertEqual(b["status"], "success")
        ahashes = [c["text_hash"] for c in a["chunks"]]
        bhashes = [c["text_hash"] for c in b["chunks"]]
        self.assertEqual(ahashes, bhashes)


if __name__ == "__main__":
    unittest.main()
