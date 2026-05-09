"""Tests for ObsidianIngestionGate."""
from __future__ import annotations

import os
import tempfile
import unittest

from spectrum_systems_core.obsidian_bridge.ingestion_gate import (
    ObsidianIngestionGate,
)


def _write(path: str, text: str) -> None:
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


class IngestionGateTests(unittest.TestCase):

    def test_valid_note_produces_success(self):
        with tempfile.TemporaryDirectory() as vault:
            note = os.path.join(vault, "Inbox", "valid.md")
            os.makedirs(os.path.dirname(note))
            _write(
                note,
                '---\ntags: ["#pending-pipeline"]\ntitle: "Sample"\n---\n'
                "Body content here.\n",
            )
            result = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(result["status"], "success")
            self.assertEqual(
                result["artifact"]["artifact_kind"],
                "obsidian_input_artifact",
            )
            self.assertTrue(
                result["artifact"]["payload"]["content_hash"].startswith(
                    "sha256:"
                )
            )

    def test_missing_trigger_tag_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            note = os.path.join(vault, "Inbox", "no-tag.md")
            os.makedirs(os.path.dirname(note))
            _write(
                note,
                '---\ntags: ["other"]\n---\nBody.\n',
            )
            result = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(result["status"], "failure")
            self.assertIn(
                "missing_trigger_tag", result["artifact"]["reason_codes"]
            )

    def test_empty_file_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            note = os.path.join(vault, "Inbox", "empty.md")
            os.makedirs(os.path.dirname(note))
            _write(note, "")
            result = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(result["status"], "failure")

    def test_nonexistent_file_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            note = os.path.join(vault, "Inbox", "missing.md")
            result = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(result["status"], "failure")
            self.assertIn(
                "unreadable_file", result["artifact"]["reason_codes"]
            )

    def test_content_hash_stable(self):
        with tempfile.TemporaryDirectory() as vault:
            note = os.path.join(vault, "Inbox", "stable.md")
            os.makedirs(os.path.dirname(note))
            _write(
                note,
                '---\ntags: ["#pending-pipeline"]\n---\nDeterministic body.\n',
            )
            first = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(first["status"], "success")
            first_hash = first["artifact"]["payload"]["content_hash"]
            # Re-run on a fresh copy with the same raw bytes; the gate
            # rewrites the file on success, so write the same content again.
            _write(
                note,
                '---\ntags: ["#pending-pipeline"]\n---\nDeterministic body.\n',
            )
            second = ObsidianIngestionGate().run(note, vault)
            self.assertEqual(second["status"], "success")
            second_hash = second["artifact"]["payload"]["content_hash"]
            self.assertEqual(first_hash, second_hash)


if __name__ == "__main__":
    unittest.main()
