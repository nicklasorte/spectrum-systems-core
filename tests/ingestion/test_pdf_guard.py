"""Tests for PDFAdmissionGuard (Phase B)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import PDFAdmissionGuard

from ._pdf_fixtures import MINIMAL_PDF, write_book_metadata


class PDFAdmissionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"
        self.source_id = "books-20260509-guard-test"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def _write_pdf(self, payload: bytes = MINIMAL_PDF) -> Path:
        target = self.store_root / "raw" / "books" / self.source_id
        target.mkdir(parents=True, exist_ok=True)
        pdf_path = target / "source.pdf"
        pdf_path.write_bytes(payload)
        return pdf_path

    def test_valid_pdf_passes_guard(self) -> None:
        write_book_metadata(self.store_root, source_id=self.source_id)
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "pass", msg=result.get("reason"))
        self.assertEqual(result["reason"], "")

    def test_missing_metadata_fails(self) -> None:
        # Directory exists but metadata.json absent.
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("metadata_missing", result["reason"])

    def test_metadata_invalid_json_fails(self) -> None:
        target = self.store_root / "raw" / "books" / self.source_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "metadata.json").write_text("not valid json {", encoding="utf-8")
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("metadata_invalid_json", result["reason"])

    def test_private_use_only_false_fails(self) -> None:
        # FINDING-B-004 regression: schema rejects private_use_only=false for books.
        write_book_metadata(
            self.store_root,
            source_id=self.source_id,
            private_use_only=False,
        )
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        # The metadata-schema check fires first because the schema's `if` clause
        # constrains `private_use_only` to true for books. Either reason code
        # is acceptable here — the point is the guard refuses, not which exact
        # check rejected first.
        self.assertTrue(
            "private_use_only_required" in result["reason"]
            or "metadata_schema_violation" in result["reason"]
        )

    def test_private_use_only_true_required_via_code(self) -> None:
        """The guard's code-level check must fire even when the schema allows it.

        Build a metadata.json that bypasses the schema's books-implies-true rule
        by claiming source_family=notes (which doesn't carry the books
        constraint), but the directory layout puts it under raw/books/. The
        guard's source_family code check rejects this, and that protects against
        a metadata payload that was somehow written under a books directory
        with private_use_only=False.
        """
        write_book_metadata(
            self.store_root,
            source_id=self.source_id,
            private_use_only=False,
            source_family="notes",
        )
        # Move metadata.json from raw/notes/<id>/ into raw/books/<id>/.
        notes_dir = self.store_root / "raw" / "notes" / self.source_id
        books_dir = self.store_root / "raw" / "books" / self.source_id
        books_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / "metadata.json").rename(books_dir / "metadata.json")
        # Patch raw_format so we don't trip the wrong_raw_format check.
        meta_path = books_dir / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["source_family"] = "notes"  # leave as notes -> wrong_source_family
        meta["raw_format"] = "pdf"
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        # The metadata declares notes; guard rejects with wrong_source_family.
        self.assertIn("wrong_source_family", result["reason"])

    def test_wrong_source_family_fails(self) -> None:
        # Place a metadata.json under raw/books/<id>/ that claims source_family
        # 'meetings'; the schema is happy but the guard rejects.
        target = self.store_root / "raw" / "books" / self.source_id
        target.mkdir(parents=True, exist_ok=True)
        metadata = {
            "source_id": self.source_id,
            "source_family": "meetings",
            "source_type": "transcript",
            "title": "Some Meeting",
            "description": "",
            "date": "2026-05-09",
            "author": "",
            "tags": [],
            "raw_format": "pdf",
            "private_use_only": True,
        }
        (target / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("wrong_source_family", result["reason"])

    def test_wrong_raw_format_fails(self) -> None:
        write_book_metadata(
            self.store_root,
            source_id=self.source_id,
            raw_format="txt",
        )
        self._write_pdf()
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("wrong_raw_format", result["reason"])

    def test_missing_pdf_fails(self) -> None:
        write_book_metadata(self.store_root, source_id=self.source_id)
        # No source.pdf written.
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("pdf_not_found", result["reason"])

    def test_invalid_magic_bytes_fails(self) -> None:
        write_book_metadata(self.store_root, source_id=self.source_id)
        self._write_pdf(payload=b"FAKE this is not a pdf")
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("invalid_pdf_magic", result["reason"])

    def test_already_extracted_fails(self) -> None:
        write_book_metadata(self.store_root, source_id=self.source_id)
        self._write_pdf()
        target = self.store_root / "raw" / "books" / self.source_id
        (target / "source.txt").write_text("prior text", encoding="utf-8")
        result = PDFAdmissionGuard().validate(self.source_id, str(self.store_root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("already_extracted", result["reason"])


if __name__ == "__main__":
    unittest.main()
