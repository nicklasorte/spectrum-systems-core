"""Tests for the Obsidian projection writer."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from spectrum_systems_core.ingestion import ObsidianProjection, SourceLoader

from ._fixtures import MEETING_TRANSCRIPT, write_source

# generated_at carries a UTC timestamp. Stripping it keeps the byte-by-byte
# comparison deterministic while still verifying the rest is regenerated.
_GENERATED_AT_RE = re.compile(
    r"^(generated_at|- Generated at): .*$", re.MULTILINE
)


def _stable(text: str) -> str:
    return _GENERATED_AT_RE.sub(lambda m: m.group(1) + ": <ts>", text)


class ObsidianProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ingest(self):
        sid = "meetings-20260509-projection"
        write_source(
            self.repo_root,
            family="meetings",
            source_id=sid,
            content=MEETING_TRANSCRIPT,
        )
        result = SourceLoader().load(sid, str(self.repo_root))
        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        return result

    def test_projection_written(self) -> None:
        result = self._ingest()
        path = ObsidianProjection().write_source_index(
            result["source_record"],
            result["text_units"],
            str(self.repo_root),
        )
        index = Path(path)
        self.assertTrue(index.is_file())
        text = index.read_text(encoding="utf-8")
        self.assertIn("meetings-20260509-projection", text)
        self.assertIn("Title for meetings-20260509-projection", text)
        self.assertIn("vault_note_status: projection", text)
        self.assertIn("Do not edit", text)

    def test_projection_regenerated(self) -> None:
        result = self._ingest()
        first = ObsidianProjection().write_source_index(
            result["source_record"],
            result["text_units"],
            str(self.repo_root),
        )
        first_text = Path(first).read_text(encoding="utf-8")
        second = ObsidianProjection().write_source_index(
            result["source_record"],
            result["text_units"],
            str(self.repo_root),
        )
        second_text = Path(second).read_text(encoding="utf-8")
        # Same input → identical Markdown (excluding the timestamp line which
        # is regenerated on every run by design).
        self.assertEqual(_stable(first_text), _stable(second_text))


if __name__ == "__main__":
    unittest.main()
