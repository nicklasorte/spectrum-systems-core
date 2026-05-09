"""Tests for the Phase A SourceLoader."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import SourceLoader

from ._fixtures import BOOK_PARAGRAPHS, MEETING_TRANSCRIPT, write_source


class SourceLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_meeting_produces_source_record(self) -> None:
        write_source(
            self.repo_root,
            family="meetings",
            source_id="meetings-20260509-q3-planning",
            content=MEETING_TRANSCRIPT,
        )
        result = SourceLoader().load(
            "meetings-20260509-q3-planning", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        record = result["source_record"]
        self.assertEqual(record["artifact_kind"], "source_record")
        self.assertTrue(record["payload"]["raw_hash"].startswith("sha256:"))
        self.assertGreater(record["payload"]["text_unit_count"], 0)
        # Speaker turns detected
        unit_types = {u["unit_type"] for u in result["text_units"]}
        self.assertEqual(unit_types, {"speaker_turn"})

    def test_valid_book_produces_paragraphs(self) -> None:
        write_source(
            self.repo_root,
            family="books",
            source_id="books-20260509-quiet-room",
            content=BOOK_PARAGRAPHS,
            metadata_overrides={
                "source_type": "book_chapter",
                "private_use_only": True,
            },
        )
        result = SourceLoader().load(
            "books-20260509-quiet-room", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        unit_types = {u["unit_type"] for u in result["text_units"]}
        self.assertEqual(unit_types, {"paragraph"})
        self.assertGreaterEqual(len(result["text_units"]), 3)

    def test_missing_source_fails(self) -> None:
        result = SourceLoader().load(
            "comments-20260509-nonexistent", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("source_not_found", result["reason"])

    def test_empty_source_fails(self) -> None:
        write_source(
            self.repo_root,
            family="comments",
            source_id="comments-20260509-empty",
            content="   \n\n   \n",
        )
        result = SourceLoader().load(
            "comments-20260509-empty", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("source_empty", result["reason"])

    def test_pdf_rejected(self) -> None:
        # Build the dir, but force raw_format=pdf in metadata.
        target = self.repo_root / "raw" / "books" / "books-20260509-pdf"
        target.mkdir(parents=True, exist_ok=True)
        (target / "source.txt").write_text("hello", encoding="utf-8")
        metadata = {
            "source_id": "books-20260509-pdf",
            "source_family": "books",
            "source_type": "book_chapter",
            "title": "Some Book",
            "description": "",
            "date": "2026-05-09",
            "author": "",
            "tags": [],
            "raw_format": "pdf",
            "private_use_only": True,
        }
        (target / "metadata.json").write_text(
            json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
        )
        result = SourceLoader().load(
            "books-20260509-pdf", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("pdf_not_supported", result["reason"])

    def test_book_requires_private_use_only_true(self) -> None:
        """Red-team regression: copyrighted material cannot ingest with
        private_use_only=False; the schema's conditional check must block."""
        write_source(
            self.repo_root,
            family="books",
            source_id="books-20260509-must-be-private",
            content=BOOK_PARAGRAPHS,
            metadata_overrides={
                "source_type": "book_chapter",
                "private_use_only": False,
            },
        )
        result = SourceLoader().load(
            "books-20260509-must-be-private", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("metadata_schema_violation", result["reason"])

    def test_invalid_metadata_fails(self) -> None:
        target = self.repo_root / "raw" / "notes" / "notes-20260509-bad"
        target.mkdir(parents=True, exist_ok=True)
        (target / "source.txt").write_text("Some content.\n", encoding="utf-8")
        # Missing 'title' and 'raw_format' required fields.
        bad_metadata = {
            "source_id": "notes-20260509-bad",
            "source_family": "notes",
            "source_type": "field_note",
            "description": "",
            "date": "2026-05-09",
            "author": "",
            "tags": [],
            "private_use_only": False,
        }
        (target / "metadata.json").write_text(
            json.dumps(bad_metadata) + "\n", encoding="utf-8"
        )
        result = SourceLoader().load(
            "notes-20260509-bad", str(self.repo_root)
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("metadata_schema_violation", result["reason"])

    def test_output_is_deterministic(self) -> None:
        sid = "working_papers-20260509-thread"
        content = "First section.\n\nSecond section line one.\nSecond section line two.\n"
        write_source(
            self.repo_root,
            family="working_papers",
            source_id=sid,
            content=content,
            metadata_overrides={"source_type": "draft_paper"},
        )

        result_a = SourceLoader().load(sid, str(self.repo_root))
        result_b = SourceLoader().load(sid, str(self.repo_root))
        self.assertEqual(result_a["status"], "success")
        self.assertEqual(result_b["status"], "success")

        payload_a = result_a["source_record"]["payload"]
        payload_b = result_b["source_record"]["payload"]
        self.assertEqual(payload_a["raw_hash"], payload_b["raw_hash"])
        self.assertEqual(payload_a["text_unit_count"], payload_b["text_unit_count"])
        self.assertEqual(payload_a["raw_path"], payload_b["raw_path"])
        self.assertEqual(payload_a["processed_path"], payload_b["processed_path"])
        # Provenance fingerprint hash is also stable for the same content.
        self.assertEqual(
            result_a["source_record"]["provenance"]["execution_fingerprint_hash"],
            result_b["source_record"]["provenance"]["execution_fingerprint_hash"],
        )
        # Text contents and ordinals match.
        texts_a = [(u["ordinal"], u["text"]) for u in result_a["text_units"]]
        texts_b = [(u["ordinal"], u["text"]) for u in result_b["text_units"]]
        self.assertEqual(texts_a, texts_b)

    def test_source_md_file_supported(self) -> None:
        sid = "notes-20260509-md-only"
        target = self.repo_root / "raw" / "notes" / sid
        target.mkdir(parents=True, exist_ok=True)
        (target / "source.md").write_text(
            "# A heading\n\nA paragraph follows.\n", encoding="utf-8"
        )
        metadata = {
            "source_id": sid,
            "source_family": "notes",
            "source_type": "field_note",
            "title": "MD Note",
            "description": "",
            "date": "2026-05-09",
            "author": "",
            "tags": [],
            "raw_format": "md",
            "private_use_only": False,
        }
        (target / "metadata.json").write_text(
            json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
        )
        result = SourceLoader().load(sid, str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertGreater(result["source_record"]["payload"]["text_unit_count"], 0)


class ExistingMeetingPipelineGuardTests(unittest.TestCase):
    """Guard test: the existing meeting transcript pipeline still imports and runs."""

    def test_existing_meeting_pipeline_still_passes(self) -> None:
        # Importing both modules confirms the public surfaces co-exist; the
        # full meeting pipeline tests live in tests/test_*.py and run as part
        # of the same suite, so simply importing here is the cheapest guard.
        from spectrum_systems_core.data_lake import (
            run_transcript_pipeline,  # noqa: F401
        )
        from spectrum_systems_core.workflows import (
            run_meeting_minutes_workflow,  # noqa: F401
        )

        result = run_meeting_minutes_workflow(
            "Quarterly planning sync\n"
            "DECISION: Approve Q3 roadmap.\n"
            "ACTION: Draft SSC-002 scope.\n"
            "QUESTION: Do we need an empty-transcript eval?\n"
        )
        self.assertTrue(result.promoted)


if __name__ == "__main__":
    unittest.main()
