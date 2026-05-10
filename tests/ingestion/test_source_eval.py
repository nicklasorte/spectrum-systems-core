"""Tests for SourceEval."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import SourceEval, SourceLoader

from ._fixtures import MEETING_TRANSCRIPT, write_source


def _result_by_name(results, name):
    for r in results:
        if r["eval_name"] == name:
            return r
    raise AssertionError(f"{name} not in results")


class SourceEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def _ingest(self, sid: str = "meetings-20260509-q3-eval"):
        write_source(
            self.store_root,
            family="meetings",
            source_id=sid,
            content=MEETING_TRANSCRIPT,
        )
        return SourceLoader().load(sid, str(self.store_root))

    def test_valid_source_allows(self) -> None:
        result = self._ingest()
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        ev = SourceEval().run(
            result["source_record"],
            result["text_units"],
            repo_root=str(self.store_root),
        )
        self.assertEqual(ev["decision"], "allow", msg=ev["reason_codes"])
        self.assertEqual(len(ev["eval_results"]), 5)
        for r in ev["eval_results"]:
            self.assertEqual(r["status"], "pass", msg=r)

    def test_empty_source_blocks(self) -> None:
        result = self._ingest()
        record = copy.deepcopy(result["source_record"])
        record["payload"]["text_unit_count"] = 0
        ev = SourceEval().run(record, [], repo_root=str(self.store_root))
        self.assertEqual(ev["decision"], "block")
        self.assertIn("source_has_no_text_units", ev["reason_codes"])
        self.assertEqual(
            _result_by_name(ev["eval_results"], "EVAL-SRC-002")["status"],
            "fail",
        )

    def test_truncated_jsonl_blocks(self) -> None:
        """Red-team regression: truncating text_units.jsonl below the stored
        text_unit_count must block on EVAL-SRC-004."""
        result = self._ingest()
        record = result["source_record"]
        # Drop the last line of the JSONL on disk; stored count stays the same.
        jsonl = (
            self.store_root
            / record["payload"]["processed_path"]
            / "text_units.jsonl"
        )
        kept = jsonl.read_text(encoding="utf-8").splitlines()[:-1]
        jsonl.write_text("\n".join(kept) + "\n", encoding="utf-8")
        ev = SourceEval().run(
            record, result["text_units"], repo_root=str(self.store_root)
        )
        self.assertEqual(ev["decision"], "block")
        self.assertEqual(
            _result_by_name(ev["eval_results"], "EVAL-SRC-004")["status"],
            "fail",
        )
        self.assertIn("text_units_unreadable", ev["reason_codes"])

    def test_hash_mismatch_blocks(self) -> None:
        result = self._ingest()
        record = copy.deepcopy(result["source_record"])
        record["payload"]["raw_hash"] = "sha256:" + "0" * 64
        ev = SourceEval().run(
            record, result["text_units"], repo_root=str(self.store_root)
        )
        self.assertEqual(ev["decision"], "block")
        self.assertIn("raw_hash_mismatch", ev["reason_codes"])
        self.assertEqual(
            _result_by_name(ev["eval_results"], "EVAL-SRC-003")["status"],
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
