"""Tests for GroundingHelper."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import GroundingHelper, SourceLoader

from ._fixtures import MEETING_TRANSCRIPT, write_source


class GroundingHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_LAKE_PATH"] = self._tmp.name
        self.store_root = Path(self._tmp.name) / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("DATA_LAKE_PATH", None)

    def _ingest(self) -> str:
        sid = "meetings-20260509-grounding"
        write_source(
            self.store_root,
            family="meetings",
            source_id=sid,
            content=MEETING_TRANSCRIPT,
        )
        result = SourceLoader().load(sid, str(self.store_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        return sid

    def test_excerpt_found(self) -> None:
        sid = self._ingest()
        result = GroundingHelper().verify_excerpt(
            "I have an update on Q3.", sid, str(self.store_root)
        )
        self.assertTrue(result["grounded"])
        self.assertGreater(len(result["matching_unit_ids"]), 0)
        self.assertTrue(result["excerpt_hash"].startswith("sha256:"))

    def test_excerpt_not_found(self) -> None:
        sid = self._ingest()
        result = GroundingHelper().verify_excerpt(
            "no such phrase appears here", sid, str(self.store_root)
        )
        self.assertFalse(result["grounded"])
        self.assertEqual(result["matching_unit_ids"], [])

    def test_missing_text_units_returns_not_grounded(self) -> None:
        result = GroundingHelper().verify_excerpt(
            "anything", "no-such-source", str(self.store_root)
        )
        self.assertFalse(result["grounded"])
        self.assertEqual(result["matching_unit_ids"], [])

    def test_find_units_by_text(self) -> None:
        sid = self._ingest()
        hits = GroundingHelper().find_units_by_text(
            "agency comments", sid, str(self.store_root)
        )
        self.assertEqual(len(hits), 1)
        self.assertIn("agency comments", hits[0]["text"])

    def test_find_units_by_text_missing_source(self) -> None:
        hits = GroundingHelper().find_units_by_text(
            "anything", "no-such-source", str(self.store_root)
        )
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
