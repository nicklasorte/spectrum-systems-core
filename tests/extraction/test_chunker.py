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


class SpeakerTurnChunkingTests(unittest.TestCase):
    """Tests for the transcript speaker-turn chunking mode."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_transcript_chunked_by_speaker_turn(self) -> None:
        texts = [
            "DiFrancisco, Michael   5:48",
            "Thanks everyone for joining.",
            "Nolen, Katrece - Contractor   5:51",
            "We can hear you.",
            "+17*******31   5:52",
            "Thank you.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-transcript-001",
            texts=texts,
        )
        result = Chunker().chunk("m-transcript-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["speaker"], "DiFrancisco, Michael")
        self.assertEqual(chunks[0]["timestamp"], "5:48")
        self.assertEqual(chunks[0]["text"], "Thanks everyone for joining.")
        self.assertEqual(chunks[1]["speaker"], "Nolen, Katrece - Contractor")
        self.assertEqual(chunks[1]["timestamp"], "5:51")
        self.assertEqual(chunks[1]["text"], "We can hear you.")
        self.assertEqual(chunks[2]["speaker"], "+17*******31")
        self.assertEqual(chunks[2]["timestamp"], "5:52")
        self.assertEqual(chunks[2]["text"], "Thank you.")
        for idx, chunk in enumerate(chunks):
            self.assertEqual(chunk["chunk_index"], idx)
            self.assertIsNone(chunk["overlap_unit_id"])
            self.assertEqual(chunk["source_id"], "m-transcript-001")

    def test_speaker_turn_merges_consecutive_lines(self) -> None:
        texts = [
            "Alice Smith   1:00",
            "First sentence of Alice's turn.",
            "Second sentence — still Alice.",
            "Third sentence, also Alice.",
            "Bob Jones   1:05",
            "Bob's only sentence.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-merge-001",
            texts=texts,
        )
        result = Chunker().chunk("m-merge-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["speaker"], "Alice Smith")
        self.assertEqual(
            chunks[0]["text"],
            "First sentence of Alice's turn.\n"
            "Second sentence — still Alice.\n"
            "Third sentence, also Alice.",
        )
        self.assertEqual(chunks[0]["unit_count"], 3)
        self.assertEqual(chunks[1]["speaker"], "Bob Jones")
        self.assertEqual(chunks[1]["text"], "Bob's only sentence.")
        self.assertEqual(chunks[1]["unit_count"], 1)

    def test_minutes_file_uses_paragraph_chunking(self) -> None:
        # Non-transcript source family + non-transcript source_id → never
        # touches speaker-turn mode, even if the text happens to look
        # speakerish.
        texts = [
            "Sarah Lee   3:00",
            "Meeting minutes paragraph one.",
            "Meeting minutes paragraph two.",
        ] + [f"Paragraph filler {i}." for i in range(20)]
        write_text_units(
            self.repo_root,
            family="notes",
            source_id="minutes-meeting-001",
            texts=texts,
        )
        result = Chunker().chunk(
            "minutes-meeting-001", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        # Character-count mode: 8 units per chunk with overlap → more than 1.
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertNotIn("speaker", chunk)
            self.assertNotIn("timestamp", chunk)

    def test_no_speaker_turns_falls_back_to_character_chunking(self) -> None:
        # Transcript family but no detectable labels → fall back to
        # character chunking and emit a warning.
        texts = [f"Plain paragraph number {i}." for i in range(20)]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-no-speakers-001",
            texts=texts,
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = Chunker().chunk(
                "m-no-speakers-001", str(self.repo_root)
            )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertGreater(len(result["chunks"]), 1)
        for chunk in result["chunks"]:
            self.assertNotIn("speaker", chunk)
            self.assertNotIn("timestamp", chunk)
        self.assertIn("no_speaker_turns_detected", buf.getvalue())

    def test_empty_speaker_turns_skipped(self) -> None:
        # The middle speaker has no content lines before the next label;
        # that turn must be skipped.
        texts = [
            "Alice Smith   1:00",
            "Alice has content.",
            "Bob Jones   1:01",
            "Carol Wu   1:02",
            "Carol has content.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-empty-001",
            texts=texts,
        )
        result = Chunker().chunk("m-empty-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 2)
        speakers = [c["speaker"] for c in chunks]
        self.assertEqual(speakers, ["Alice Smith", "Carol Wu"])

    def test_all_speaker_turns_empty_falls_back_to_character_chunking(
        self,
    ) -> None:
        # Gate B Sev-2 guard: when every detected label has zero content
        # lines (e.g., a timestamp-only transcript), the chunker must NOT
        # emit zero chunks — that would break the orchestrator's Stage 2
        # artifact-existence signal. It must fall back to character
        # chunking and print a distinct warning identifying this case.
        texts = [
            "Alice Smith   1:00",
            "Bob Jones   1:01",
            "Carol Wu   1:02",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-all-empty-001",
            texts=texts,
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = Chunker().chunk(
                "m-all-empty-001", str(self.repo_root)
            )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        # Must emit ≥1 chunk so chunks.jsonl is non-empty (Stage 2 signal).
        self.assertGreaterEqual(len(result["chunks"]), 1)
        # Character-mode chunks have no speaker field.
        for chunk in result["chunks"]:
            self.assertNotIn("speaker", chunk)
        # The warning must distinguish this case from "no labels at all".
        self.assertIn("all_speaker_turns_empty", buf.getvalue())
        self.assertNotIn("no_speaker_turns_detected", buf.getvalue())

    def test_phone_number_speaker_detected(self) -> None:
        texts = [
            "+17*******31   4:00",
            "Caller statement.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-phone-001",
            texts=texts,
        )
        result = Chunker().chunk("m-phone-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertEqual(len(result["chunks"]), 1)
        chunk = result["chunks"][0]
        self.assertEqual(chunk["speaker"], "+17*******31")
        self.assertEqual(chunk["timestamp"], "4:00")
        self.assertEqual(chunk["text"], "Caller statement.")

    def test_character_chunks_validate_against_updated_schema(self) -> None:
        # Gate A Sev-2 guard: existing non-transcript chunks must still
        # validate against chunk.schema.json after the schema gained the
        # optional ``speaker`` and ``timestamp`` properties.
        import jsonschema

        from spectrum_systems_core.ingestion._paths import schema_path

        schema = json.loads(
            schema_path("chunk").read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema)

        texts = [f"Paragraph {i} text content here." for i in range(20)]
        write_text_units(
            self.repo_root,
            family="notes",
            source_id="ns-schema-compat",
            texts=texts,
        )
        result = Chunker().chunk("ns-schema-compat", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertGreater(len(result["chunks"]), 1)
        for chunk in result["chunks"]:
            validator.validate(chunk)  # raises on failure

    def test_regex_does_not_fire_on_two_space_gap(self) -> None:
        # Gate A Sev-2 guard: a 2-space gap before HH:MM (common in normal
        # prose, e.g., "See you at  3:00") must NOT be detected as a
        # speaker boundary.
        texts = [
            "See you at  3:00",
            "I'll meet you there.",
            "Another paragraph  4:00 close",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-noise-001",
            texts=texts,
        )
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = Chunker().chunk("m-noise-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        # No labels detected → fallback warning + character chunks.
        self.assertIn("no_speaker_turns_detected", buf.getvalue())
        for chunk in result["chunks"]:
            self.assertNotIn("speaker", chunk)

    def test_regex_does_not_fire_on_colon_in_speaker_portion(self) -> None:
        # Gate A Sev-2 guard: lines with a colon before the HH:MM gap
        # (e.g., "Action: Bob   4:00") must NOT be detected as a speaker
        # boundary. Real speaker labels don't have colons.
        texts = [
            "Alice Smith   1:00",
            "Action: Bob   4:00",
            "Some other content here.",
            "Carol Wu   1:05",
            "Carol's statement.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-action-001",
            texts=texts,
        )
        result = Chunker().chunk("m-action-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        # Two real speakers; "Action: Bob   4:00" must be content of Alice's turn.
        self.assertEqual(len(chunks), 2)
        speakers = [c["speaker"] for c in chunks]
        self.assertEqual(speakers, ["Alice Smith", "Carol Wu"])
        # Alice's turn should include the action line as content.
        self.assertIn("Action: Bob   4:00", chunks[0]["text"])

    def test_tab_separator_detected(self) -> None:
        # Real docx exports often render the speaker / timestamp gap as a
        # tab character. The regex must accept a tab as the separator.
        texts = [
            "Alice Smith\t1:00",
            "Alice content.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-tab-001",
            texts=texts,
        )
        result = Chunker().chunk("m-tab-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertEqual(len(result["chunks"]), 1)
        self.assertEqual(result["chunks"][0]["speaker"], "Alice Smith")
        self.assertEqual(result["chunks"][0]["timestamp"], "1:00")

    def test_transcript_source_id_triggers_speaker_mode_in_any_family(self) -> None:
        # Non-meetings family but "transcript" in source_id → speaker mode.
        texts = [
            "Alice Smith   1:00",
            "Alice content.",
            "Bob Jones   1:05",
            "Bob content.",
        ]
        write_text_units(
            self.repo_root,
            family="notes",
            source_id="research-transcript-2026-01-15",
            texts=texts,
        )
        result = Chunker().chunk(
            "research-transcript-2026-01-15", str(self.repo_root)
        )
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        chunks = result["chunks"]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["speaker"], "Alice Smith")
        self.assertEqual(chunks[1]["speaker"], "Bob Jones")


if __name__ == "__main__":
    unittest.main()
