"""Tests for PDFExtractor (Phase B)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import PDFExtractor

from ._pdf_fixtures import MINIMAL_PDF, RICH_PDF, write_book_metadata


class PDFExtractorSuccessTests(unittest.TestCase):
    """Use RICH_PDF (>500 chars extracted) for the success path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "books-20260509-rich"
        write_book_metadata(self.repo_root, source_id=self.source_id)
        target = self.repo_root / "raw" / "books" / self.source_id
        (target / "source.pdf").write_bytes(RICH_PDF)
        self.target = target

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> dict:
        return PDFExtractor().extract(self.source_id, str(self.repo_root))

    def test_extraction_produces_source_txt(self) -> None:
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        txt_path = self.target / "source.txt"
        self.assertTrue(txt_path.is_file())
        self.assertGreater(len(txt_path.read_text(encoding="utf-8")), 500)

    def test_extraction_produces_pages_jsonl(self) -> None:
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        pages_path = self.target / "pages.jsonl"
        self.assertTrue(pages_path.is_file())
        lines = pages_path.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 1)
        for idx, line in enumerate(lines, start=1):
            entry = json.loads(line)
            self.assertEqual(entry["page_number"], idx)
            self.assertIsInstance(entry["page_number"], int)
            self.assertEqual(entry["source_id"], self.source_id)
            self.assertTrue(entry["text_hash"].startswith("sha256:"))

    def test_extraction_report_written(self) -> None:
        result = self._run()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        report_path = self.target / "extraction_report.json"
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "success")
        self.assertTrue(report["pdf_magic_valid"])
        self.assertTrue(report["private_use_only_verified"])
        self.assertFalse(report["scanned_pdf_suspected"])
        self.assertEqual(report["source_family"], "books")
        self.assertEqual(report["extraction_library"], "pdfminer.six")

    def test_extracted_text_hash_recorded(self) -> None:
        # FINDING-B-001 regression.
        result = self._run()
        self.assertEqual(result["status"], "success")
        report = result["extraction_report"]
        self.assertTrue(report["extracted_text_hash"].startswith("sha256:"))
        self.assertEqual(len(report["extracted_text_hash"].split(":")[1]), 64)
        self.assertTrue(report["extraction_library_version"])

    def test_page_numbers_are_authoritative(self) -> None:
        # FINDING-B-003 regression.
        result = self._run()
        self.assertEqual(result["status"], "success")
        pages_path = self.target / "pages.jsonl"
        entries = [
            json.loads(line)
            for line in pages_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(
            [e["page_number"] for e in entries],
            list(range(1, len(entries) + 1)),
        )
        for entry in entries:
            self.assertIn("char_start_advisory", entry)
            self.assertIn("char_end_advisory", entry)

    def test_extraction_is_deterministic(self) -> None:
        # FINDING-B-001 regression.
        first = self._run()
        self.assertEqual(first["status"], "success")
        first_hash = first["extraction_report"]["extracted_text_hash"]

        # Guard blocks a re-run unless source.txt is removed first.
        (self.target / "source.txt").unlink()
        (self.target / "pages.jsonl").unlink()
        (self.target / "extraction_report.json").unlink()

        second = self._run()
        self.assertEqual(second["status"], "success", msg=second.get("reason"))
        second_hash = second["extraction_report"]["extracted_text_hash"]
        self.assertEqual(first_hash, second_hash)


class PDFExtractorFailureTests(unittest.TestCase):
    """Failure paths use MINIMAL_PDF (extracts ~11 chars, below threshold)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.source_id = "books-20260509-minimal"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scanned_pdf_blocked(self) -> None:
        # FINDING-B-002 regression.
        write_book_metadata(self.repo_root, source_id=self.source_id)
        target = self.repo_root / "raw" / "books" / self.source_id
        (target / "source.pdf").write_bytes(MINIMAL_PDF)

        result = PDFExtractor().extract(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "scanned_pdf_suspected")
        report = result["extraction_report"]
        self.assertTrue(report["scanned_pdf_suspected"])
        self.assertEqual(report["status"], "failure")
        # source.txt and pages.jsonl must NOT be written.
        self.assertFalse((target / "source.txt").exists())
        self.assertFalse((target / "pages.jsonl").exists())
        # But extraction_report.json IS written, so operators see why.
        self.assertTrue((target / "extraction_report.json").is_file())

    def test_guard_failure_propagates_through_extractor(self) -> None:
        # No metadata.json, no source.pdf — guard rejects, extractor returns
        # failure cleanly without creating outputs.
        target = self.repo_root / "raw" / "books" / self.source_id
        target.mkdir(parents=True, exist_ok=True)

        result = PDFExtractor().extract(self.source_id, str(self.repo_root))
        self.assertEqual(result["status"], "failure")
        self.assertIn("metadata_missing", result["reason"])
        self.assertFalse((target / "source.txt").exists())
        self.assertFalse((target / "pages.jsonl").exists())

    def test_failed_extraction_does_not_write_projection(self) -> None:
        # Runtime red-team check: a failed extraction must not produce a
        # processed/books/<id>/markdown/index.md projection. The extractor
        # itself only writes to raw/; projection is the CLI's job. This test
        # verifies that the extractor stays out of processed/.
        write_book_metadata(self.repo_root, source_id=self.source_id)
        target = self.repo_root / "raw" / "books" / self.source_id
        (target / "source.pdf").write_bytes(MINIMAL_PDF)

        PDFExtractor().extract(self.source_id, str(self.repo_root))

        processed_md = (
            self.repo_root
            / "processed"
            / "books"
            / self.source_id
            / "markdown"
            / "index.md"
        )
        self.assertFalse(
            processed_md.exists(),
            "extractor must not write processed/markdown/index.md",
        )


if __name__ == "__main__":
    unittest.main()
