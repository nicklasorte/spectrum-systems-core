"""End-to-end test for the `prepare-pdf` CLI command (Phase B).

Verifies the two-step Phase A / Phase B boundary (FINDING-B-005):
- prepare-pdf must succeed without invoking process-source
- the Markdown projection is written only on success
- the projection lives under processed/books/<id>/markdown/index.md
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.cli import prepare_pdf

from ._pdf_fixtures import MINIMAL_PDF, RICH_PDF, write_book_metadata


class PreparePdfCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prepare_pdf_succeeds_for_rich_pdf(self) -> None:
        source_id = "books-20260509-rich-cli"
        write_book_metadata(self.repo_root, source_id=source_id)
        target = self.repo_root / "raw" / "books" / source_id
        (target / "source.pdf").write_bytes(RICH_PDF)

        out = io.StringIO()
        rc = prepare_pdf(
            source_id=source_id, repo_root=self.repo_root, out_stream=out
        )
        self.assertEqual(rc, 0, msg=out.getvalue())

        # Phase B output files exist on disk.
        self.assertTrue((target / "source.txt").is_file())
        self.assertTrue((target / "pages.jsonl").is_file())
        self.assertTrue((target / "extraction_report.json").is_file())

        # Markdown projection landed under processed/, not raw/.
        projection = (
            self.repo_root
            / "processed"
            / "books"
            / source_id
            / "markdown"
            / "index.md"
        )
        self.assertTrue(projection.is_file())
        body = projection.read_text(encoding="utf-8")
        self.assertIn("PRIVATE USE ONLY", body)
        self.assertIn("process-source", body)

        # FINDING-B-005: prepare-pdf must NOT invoke process-source. So the
        # Phase A artifacts (source_record.json, text_units.jsonl) must NOT
        # exist yet.
        processed_dir = (
            self.repo_root / "processed" / "books" / source_id
        )
        self.assertFalse((processed_dir / "source_record.json").exists())
        self.assertFalse((processed_dir / "text_units.jsonl").exists())

        # CLI output mentions the next step explicitly.
        printed = out.getvalue()
        self.assertIn("Next step:", printed)
        self.assertIn("process-source", printed)

    def test_prepare_pdf_fails_for_scanned_pdf_and_skips_projection(self) -> None:
        # Runtime red-team check: a failure must NOT produce a Markdown
        # projection (otherwise a failed extraction looks like a success).
        source_id = "books-20260509-scanned-cli"
        write_book_metadata(self.repo_root, source_id=source_id)
        target = self.repo_root / "raw" / "books" / source_id
        (target / "source.pdf").write_bytes(MINIMAL_PDF)

        out = io.StringIO()
        rc = prepare_pdf(
            source_id=source_id, repo_root=self.repo_root, out_stream=out
        )
        self.assertEqual(rc, 1)
        self.assertIn("scanned_pdf_suspected", out.getvalue())

        projection = (
            self.repo_root
            / "processed"
            / "books"
            / source_id
            / "markdown"
            / "index.md"
        )
        self.assertFalse(projection.exists())

    def test_prepare_pdf_idempotency_blocked_by_guard(self) -> None:
        # Runtime red-team check: re-running prepare-pdf on the same source
        # must be blocked by the guard (already_extracted) rather than
        # silently overwriting source.txt.
        source_id = "books-20260509-rerun"
        write_book_metadata(self.repo_root, source_id=source_id)
        target = self.repo_root / "raw" / "books" / source_id
        (target / "source.pdf").write_bytes(RICH_PDF)

        first = prepare_pdf(
            source_id=source_id,
            repo_root=self.repo_root,
            out_stream=io.StringIO(),
        )
        self.assertEqual(first, 0)

        out = io.StringIO()
        second = prepare_pdf(
            source_id=source_id, repo_root=self.repo_root, out_stream=out
        )
        self.assertEqual(second, 1)
        self.assertIn("already_extracted", out.getvalue())


if __name__ == "__main__":
    unittest.main()
