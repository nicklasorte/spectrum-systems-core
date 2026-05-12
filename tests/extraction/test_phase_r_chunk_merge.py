"""Phase R.0: minimum-chunk-size merge tests.

Pipeline #23 revealed that 2-68 char chunks were producing empty
extraction responses. The merge pass runs in the Chunker before
chunks.jsonl is written, and the merge_short_chunks helper is the
pure-function entry point unit-tested here.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from spectrum_systems_core.extraction import (
    CHUNK_MERGE_ENABLED_ENV,
    Chunker,
    MIN_CHUNK_CHARS,
    MIN_CHUNK_CHARS_ENV,
    merge_short_chunks,
)
from spectrum_systems_core.validation import validate_artifact

from ._fixtures import write_text_units


def _hash() -> str:
    return "sha256:" + ("a" * 64)


def _chunk(
    *,
    text: str,
    index: int,
    unit_ids: Optional[List[str]] = None,
    speaker: Optional[str] = None,
    agenda_item_id: Optional[str] = None,
) -> Dict[str, Any]:
    units = unit_ids if unit_ids is not None else [str(uuid.uuid4())]
    chunk: Dict[str, Any] = {
        "chunk_id": str(uuid.uuid4()),
        "source_id": "src-0001",
        "source_family": "meetings",
        "chunk_index": index,
        "unit_ids": list(units),
        "text": text,
        "text_hash": _hash(),
        "unit_count": len(units),
        "overlap_unit_id": None,
        "page_numbers": [],
        "char_count": len(text),
    }
    if speaker is not None:
        chunk["speaker"] = speaker
    if agenda_item_id is not None:
        chunk["agenda_item_id"] = agenda_item_id
    return chunk


@contextmanager
def _env(**vars: str) -> Iterator[None]:
    """Temporarily set/clear environment variables."""
    previous: Dict[str, Optional[str]] = {}
    for k, v in vars.items():
        previous[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, prev in previous.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


class MergeShortChunksTests(unittest.TestCase):
    """Unit tests for the pure ``merge_short_chunks`` helper."""

    def test_short_chunk_merges_into_preceding(self) -> None:
        a = _chunk(text="A" * 200, index=0, speaker="Alice")
        b = _chunk(text="Oh", index=1, speaker="Bob")  # 2 chars
        out, pairs = merge_short_chunks([a, b], min_chars=150)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["chunk_id"], a["chunk_id"])
        self.assertEqual(out[0]["speaker"], "Alice", "speaker comes from first component")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["absorbed_chunk_id"], b["chunk_id"])
        self.assertEqual(pairs[0]["into_chunk_id"], a["chunk_id"])
        self.assertEqual(pairs[0]["reason"], "below_min_chars")

    def test_short_chunk_at_position_zero_merges_into_following(self) -> None:
        a = _chunk(text="I.", index=0, speaker="Alice")  # 2 chars; first
        b = _chunk(text="B" * 200, index=1, speaker="Bob")
        out, pairs = merge_short_chunks([a, b], min_chars=150)
        self.assertEqual(len(out), 1)
        # Survivor identity comes from the first component (the short one).
        self.assertEqual(out[0]["chunk_id"], a["chunk_id"])
        self.assertEqual(out[0]["speaker"], "Alice")
        # The absorbed in this direction is the second chunk.
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["absorbed_chunk_id"], b["chunk_id"])
        self.assertEqual(pairs[0]["into_chunk_id"], a["chunk_id"])

    def test_chunk_at_exactly_min_chars_not_merged(self) -> None:
        threshold = 150
        a = _chunk(text="A" * threshold, index=0, speaker="Alice")
        b = _chunk(text="B" * threshold, index=1, speaker="Bob")
        out, pairs = merge_short_chunks([a, b], min_chars=threshold)
        self.assertEqual(len(out), 2)
        self.assertEqual(pairs, [])

    def test_chunk_at_threshold_minus_one_does_merge(self) -> None:
        threshold = 150
        short = _chunk(text="A" * (threshold - 1), index=0, speaker="Alice")
        big = _chunk(text="B" * threshold, index=1, speaker="Bob")
        out, pairs = merge_short_chunks([short, big], min_chars=threshold)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(pairs), 1)

    def test_two_adjacent_short_chunks_collapse(self) -> None:
        a = _chunk(text="Oh", index=0, speaker="Alice")
        b = _chunk(text="I.", index=1, speaker="Bob")
        c = _chunk(text="Awesome.", index=2, speaker="Carol")
        # Threshold = 150 -- each chunk is well below it.
        out, pairs = merge_short_chunks([a, b, c], min_chars=150)
        self.assertEqual(len(out), 1)
        # Merge order: at idx=1 first (no agenda boundary, prev exists) →
        # merged with a, leaving [ab, c]. Then c is short, merged with ab.
        self.assertEqual(out[0]["chunk_id"], a["chunk_id"])
        self.assertEqual(out[0]["speaker"], "Alice")
        # Two absorptions happened.
        self.assertEqual(len(pairs), 2)

    def test_merged_chunk_unit_ids_is_union_of_both_components(self) -> None:
        # Distinct unit_ids on each side: the union must contain ALL of
        # them, in order.
        a_units = ["u-1", "u-2"]
        b_units = ["u-3", "u-4", "u-5"]
        a = _chunk(text="X" * 200, index=0, unit_ids=a_units, speaker="Alice")
        b = _chunk(text="!", index=1, unit_ids=b_units, speaker="Bob")
        out, _ = merge_short_chunks([a, b], min_chars=150)
        self.assertEqual(len(out), 1)
        merged_units = out[0]["unit_ids"]
        # Must contain ALL unit_ids from both components (rule 6).
        for uid in a_units + b_units:
            self.assertIn(uid, merged_units)
        self.assertEqual(out[0]["unit_count"], len(merged_units))
        # No duplicates.
        self.assertEqual(len(merged_units), len(set(merged_units)))

    def test_agenda_boundary_blocks_merge(self) -> None:
        # Two short chunks on opposite sides of an agenda boundary
        # must NOT merge -- the rule 4 guard.
        a = _chunk(
            text="Oh.", index=0, speaker="Alice",
            agenda_item_id="agenda-1",
        )
        b = _chunk(
            text="Hello.", index=1, speaker="Bob",
            agenda_item_id="agenda-2",
        )
        out, pairs = merge_short_chunks([a, b], min_chars=150)
        # No safe merge partner -- merger stops at the boundary.
        self.assertEqual(len(out), 2)
        self.assertEqual(pairs, [])

    def test_chunk_index_is_sequential_after_merge(self) -> None:
        a = _chunk(text="A" * 200, index=0)
        b = _chunk(text="!", index=1)
        c = _chunk(text="C" * 200, index=2)
        out, _ = merge_short_chunks([a, b, c], min_chars=150)
        self.assertEqual([c_["chunk_index"] for c_ in out], list(range(len(out))))

    def test_no_chunk_below_min_after_merge(self) -> None:
        # After the merge pass, every chunk must be >= min_chars OR a
        # singleton that could not find a partner (agenda boundary on
        # both sides).
        a = _chunk(text="Oh.", index=0)
        b = _chunk(text="!", index=1)
        c = _chunk(text="Hi.", index=2)
        out, _ = merge_short_chunks([a, b, c], min_chars=150)
        # All collapse into one chunk; total chars 8 (= "Oh.\n!\nHi."),
        # still < 150 because all components are short. But there is no
        # neighbour left to absorb -- the merge loop terminates with
        # a single chunk below threshold. This is the documented "all
        # filler" edge case: it still produces ONE chunk, never zero.
        # The downstream guard (Phase X guard_empty_response) will
        # handle the empty response if the model returns nothing.
        self.assertEqual(len(out), 1)


class ChunkerMergeIntegrationTests(unittest.TestCase):
    """Integration tests for the merge pass running inside Chunker.chunk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_chunk_merge_summary_artifact_written(self) -> None:
        # Speaker-turn transcript with one big and one tiny turn.
        texts = [
            "Alice Smith   1:00",
            "A" * 250,
            "Bob Jones   1:01",
            "Oh.",  # 3 chars -- triggers merge into prev
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-001",
            texts=texts,
        )
        result = Chunker().chunk("m-r0-merge-001", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        # Exactly one absorption happened (Bob -> Alice).
        self.assertEqual(result["chunks_merged"], 1)
        self.assertEqual(result["original_chunk_count"], 2)
        self.assertEqual(result["merged_chunk_count"], 1)

        # The summary artifact lives next to chunks.jsonl.
        summary_path = (
            self.repo_root / "processed" / "meetings" / "m-r0-merge-001"
            / "stories" / "chunk_merge_summary.json"
        )
        self.assertTrue(summary_path.is_file())
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(data["artifact_type"], "chunk_merge_summary")
        self.assertEqual(data["schema_version"], "1.0.0")
        self.assertEqual(data["source_id"], "m-r0-merge-001")
        self.assertEqual(data["min_chunk_chars"], MIN_CHUNK_CHARS)
        self.assertEqual(data["chunks_merged"], 1)
        self.assertEqual(len(data["merge_pairs"]), 1)

    def test_chunk_merge_summary_passes_schema_validation(self) -> None:
        texts = [
            "Alice Smith   1:00",
            "A" * 250,
            "Bob Jones   1:01",
            "Oh.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-002",
            texts=texts,
        )
        Chunker().chunk("m-r0-merge-002", str(self.repo_root))
        summary_path = (
            self.repo_root / "processed" / "meetings" / "m-r0-merge-002"
            / "stories" / "chunk_merge_summary.json"
        )
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        # Must validate against the central schema gate. Raises if not.
        validate_artifact(data, "chunk_merge_summary")

    def test_min_chunk_chars_read_from_env(self) -> None:
        # Without env override: 200-char chunk is kept; 100-char short
        # one is absorbed (default threshold 150).
        # With env override: threshold raised to 300, the 200-char chunk
        # ALSO becomes "short" and is absorbed.
        texts = [
            "Alice Smith   1:00",
            "A" * 200,
            "Bob Jones   1:01",
            "B" * 200,
            "Carol Wu   1:02",
            "C" * 200,
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-003",
            texts=texts,
        )
        with _env(MIN_CHUNK_CHARS="300"):
            result = Chunker().chunk(
                "m-r0-merge-003", str(self.repo_root),
            )
        self.assertEqual(result["status"], "success")
        # Without merge (env=1) we would see 3 chunks. With env=300 they
        # all fall under threshold and collapse to 1.
        self.assertEqual(result["merged_chunk_count"], 1)

    def test_chunks_below_min_eliminated_after_merge(self) -> None:
        # The 4 short chunks present in pipeline #23 (sizes 68/3/2/8)
        # would, in pre-R.0 code, all survive. With the merge pass on,
        # none remain below threshold so long as a partner exists.
        texts = [
            "Salim Reza   1:00",
            "Good morning. This is Salim and I'm chairing today's meeting.",  # 65 chars
            "Alice Smith   1:01",
            "Oh.",
            "Bob Jones   1:02",
            "I.",
            "Carol Wu   1:03",
            "Awesome.",
            "Dave Lee   1:04",
            "X" * 250,  # anchor chunk
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-004",
            texts=texts,
        )
        result = Chunker().chunk("m-r0-merge-004", str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        # No survivor chunk should remain below MIN_CHUNK_CHARS (since
        # a partner exists for each short chunk).
        for chunk in result["chunks"]:
            self.assertGreaterEqual(
                chunk["char_count"], MIN_CHUNK_CHARS,
                msg=f"chunk {chunk['chunk_id']} below threshold after merge",
            )

    def test_merge_disabled_via_env(self) -> None:
        # CHUNK_MERGE_ENABLED=false skips the merge pass entirely.
        texts = [
            "Alice Smith   1:00",
            "A" * 250,
            "Bob Jones   1:01",
            "Oh.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-005",
            texts=texts,
        )
        with _env(CHUNK_MERGE_ENABLED="false"):
            result = Chunker().chunk(
                "m-r0-merge-005", str(self.repo_root),
            )
        self.assertEqual(result["status"], "success")
        # Both chunks survive because the merge pass was off.
        self.assertEqual(result["chunks_merged"], 0)
        self.assertEqual(result["merged_chunk_count"], 2)

    def test_merge_runs_before_chunks_jsonl_write(self) -> None:
        # Critical correctness check: chunks.jsonl on disk must reflect
        # the MERGED chunk list, not the pre-merge list.
        texts = [
            "Alice Smith   1:00",
            "A" * 250,
            "Bob Jones   1:01",
            "Oh.",
        ]
        write_text_units(
            self.repo_root,
            family="meetings",
            source_id="m-r0-merge-006",
            texts=texts,
        )
        Chunker().chunk("m-r0-merge-006", str(self.repo_root))
        chunks_path = (
            self.repo_root / "processed" / "meetings" / "m-r0-merge-006"
            / "stories" / "chunks.jsonl"
        )
        with chunks_path.open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "chunks.jsonl must contain the merged chunk only")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
