"""Tests for VaultIndexWriter."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from spectrum_systems_core.obsidian_bridge.vault_index_writer import (
    VaultIndexWriter,
)
from spectrum_systems_core.obsidian_bridge import _frontmatter


def _seed_source_note(vault_root: str, source_rel: str) -> None:
    abs_path = os.path.join(vault_root, source_rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as fh:
        fh.write(b'---\ntitle: "src"\n---\nbody\n')


def _seed_data_lake(sdl_root: str, artifact_id: str) -> None:
    os.makedirs(sdl_root, exist_ok=True)
    with open(os.path.join(sdl_root, artifact_id + ".json"), "w") as fh:
        json.dump({"artifact_id": artifact_id}, fh)


def _common_kwargs(vault_root: str, artifact_id: str) -> dict:
    return dict(
        promoted_artifact_id=artifact_id,
        artifact_type="decision_brief",
        schema_version="1.0.0",
        promoted_at="2026-05-09T12:00:00Z",
        pipeline_run_id="run-1",
        data_lake_ref=f"sdl://{artifact_id}",
        source_note_vault_path="Inbox/source.md",
        eval_results=[
            {"metric_name": "obsidian_input_artifact.schema_valid"},
        ],
        reviewer_id="reviewer-1",
        review_decision="approve",
        artifact_summary="Sample summary.",
        vault_root=vault_root,
    )


class VaultIndexWriterTests(unittest.TestCase):

    def test_successful_write(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as sdl:
            artifact_id = str(uuid.uuid4())
            _seed_source_note(vault, "Inbox/source.md")
            _seed_data_lake(sdl, artifact_id)
            with mock.patch.dict(os.environ, {"SDL_ROOT": sdl}):
                result = VaultIndexWriter().write_index_note(
                    **_common_kwargs(vault, artifact_id)
                )
            self.assertEqual(result["status"], "success", result)
            self.assertTrue(os.path.exists(result["index_note_path"]))
            with open(result["index_note_path"], "rb") as fh:
                fm, _body = _frontmatter.split(fh.read().decode("utf-8"))
            self.assertEqual(fm["artifact_id"], artifact_id)

    def test_idempotent_skip(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as sdl:
            artifact_id = str(uuid.uuid4())
            _seed_source_note(vault, "Inbox/source.md")
            _seed_data_lake(sdl, artifact_id)
            with mock.patch.dict(os.environ, {"SDL_ROOT": sdl}):
                first = VaultIndexWriter().write_index_note(
                    **_common_kwargs(vault, artifact_id)
                )
                second = VaultIndexWriter().write_index_note(
                    **_common_kwargs(vault, artifact_id)
                )
            self.assertEqual(first["status"], "success")
            self.assertEqual(second["status"], "skipped")

    def test_missing_data_lake_ref_fails(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as sdl:
            artifact_id = str(uuid.uuid4())
            _seed_source_note(vault, "Inbox/source.md")
            # Note: do NOT seed the artifact in sdl_root.
            with mock.patch.dict(os.environ, {"SDL_ROOT": sdl}):
                result = VaultIndexWriter().write_index_note(
                    **_common_kwargs(vault, artifact_id)
                )
            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["reason"], "data_lake_ref_not_resolvable")


if __name__ == "__main__":
    unittest.main()
