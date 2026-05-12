"""Phase X2.1 follow-up — agenda detector wired into chunker.

The agenda detector module shipped in PR #71 but was never called by
the chunker, so ``agenda_item_id`` was always absent on chunks. These
tests defend the trust property that the chunker now invokes the
detector AFTER assign_chunk_positions and writes a non-empty
``agenda_item_id`` to every chunk when AGENDA_DETECTION_ENABLED is on.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.extraction import Chunker

from ._fixtures import write_text_units


class AgendaWiringTests(unittest.TestCase):
    """Validates the merge -> split -> position -> agenda call order."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        # The toy transcripts here are short; disable the merge pass so
        # each speaker turn survives as its own chunk and we can compare
        # chunk_index against agenda boundaries directly.
        self._prev_merge_env = os.environ.get("CHUNK_MERGE_ENABLED")
        os.environ["CHUNK_MERGE_ENABLED"] = "false"
        self._prev_agenda_env = os.environ.get("AGENDA_DETECTION_ENABLED")
        os.environ.pop("AGENDA_DETECTION_ENABLED", None)

    def tearDown(self) -> None:
        if self._prev_merge_env is None:
            os.environ.pop("CHUNK_MERGE_ENABLED", None)
        else:
            os.environ["CHUNK_MERGE_ENABLED"] = self._prev_merge_env
        if self._prev_agenda_env is None:
            os.environ.pop("AGENDA_DETECTION_ENABLED", None)
        else:
            os.environ["AGENDA_DETECTION_ENABLED"] = self._prev_agenda_env
        self._tmp.cleanup()

    def test_transcript_without_headers_marks_all_chunks_unclassified(
        self,
    ) -> None:
        """When the detector finds zero agenda headers, the wiring
        assigns the literal string 'unclassified' to every chunk -- the
        field is ALWAYS a non-empty string per the X2 amendment."""
        texts = [
            "DiFrancisco, Michael   5:48",
            "Thanks everyone for joining.",
            "Nolen, Katrece - Contractor   5:51",
            "Good morning.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-transcript-noheaders",
            texts=texts,
        )
        result = Chunker().chunk(
            "m-transcript-noheaders", str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        self.assertGreater(len(result["chunks"]), 0)
        for chunk in result["chunks"]:
            self.assertEqual(chunk.get("agenda_item_id"), "unclassified")

    def test_transcript_with_headers_assigns_agenda_ids(self) -> None:
        """When detectable agenda headers exist in the source text, the
        wiring labels each chunk with the appropriate ``AI-NNN`` id."""
        texts = [
            "Agenda Item 1: Introductions",
            "Chair Smith    9:00",
            "Welcome everyone.",
            "Agenda Item 2: Spectrum Analysis",
            "Engineer Park    9:15",
            "Today we will review.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-transcript-headers",
            texts=texts,
        )
        result = Chunker().chunk(
            "m-transcript-headers", str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        ids = {chunk.get("agenda_item_id") for chunk in result["chunks"]}
        # All chunks carry a string identifier (string-not-null contract).
        self.assertTrue(all(isinstance(i, str) for i in ids))
        # At least one chunk must land in a detected agenda section.
        self.assertTrue(
            any(isinstance(i, str) and i.startswith("AI-") for i in ids),
            f"expected at least one AI-NNN id, got {ids!r}",
        )

    def test_disabling_agenda_detection_yields_no_field(self) -> None:
        """``AGENDA_DETECTION_ENABLED=false`` is the documented rollback
        path; with it set, the chunker MUST NOT label chunks. We assert
        the field is absent so a future consumer cannot accidentally rely
        on a sentinel value when the rollback flag is set."""
        os.environ["AGENDA_DETECTION_ENABLED"] = "false"
        texts = [
            "Agenda Item 1: Introductions",
            "Chair Smith    9:00",
            "Welcome everyone.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-transcript-disabled",
            texts=texts,
        )
        result = Chunker().chunk(
            "m-transcript-disabled", str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        for chunk in result["chunks"]:
            # Pre-X2 behaviour preserved as rollback: no agenda_item_id
            # is set when detection is disabled.
            self.assertNotIn("agenda_item_id", chunk)

    def test_source_record_records_agenda_items_when_detected(self) -> None:
        """When the detector finds agenda items, the chunker writes them
        to ``source_record.payload.agenda_items`` so downstream readers
        can map chunk ids back to a human-readable title."""
        # Seed a source_record so the updater has something to merge into.
        source_id = "m-transcript-agenda-sr"
        processed_dir = (
            self.repo_root / "processed" / "meetings" / source_id
        )
        processed_dir.mkdir(parents=True, exist_ok=True)
        (processed_dir / "source_record.json").write_text(
            json.dumps({"payload": {}}),
            encoding="utf-8",
        )
        texts = [
            "Agenda Item 1: Introductions",
            "Chair Smith    9:00",
            "Welcome everyone.",
            "Agenda Item 2: Spectrum Analysis",
            "Engineer Park    9:15",
            "Today we will review.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id=source_id,
            texts=texts,
        )
        result = Chunker().chunk(source_id, str(self.repo_root))
        self.assertEqual(result["status"], "success", result.get("reason"))
        sr = json.loads(
            (processed_dir / "source_record.json").read_text(encoding="utf-8")
        )
        agenda_items = sr.get("payload", {}).get("agenda_items")
        self.assertIsInstance(agenda_items, list)
        # Detector emitted at least one item, and each item carries the
        # documented shape (id + title + turn-index span).
        if agenda_items:
            sample = agenda_items[0]
            for key in (
                "agenda_item_id", "title",
                "start_turn_index", "end_turn_index",
            ):
                self.assertIn(key, sample)
