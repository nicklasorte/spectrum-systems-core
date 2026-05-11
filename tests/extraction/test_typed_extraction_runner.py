"""End-to-end integration tests for typed_extraction_runner.

Phase M3 C4. Drives the full chunk -> classify -> route -> extract ->
merge -> write pipeline with stubbed LLM callers. Verifies:
- A normal source produces a meeting_extraction artifact on disk.
- Re-running without --force skips (idempotency).
- Re-running with force=True overwrites.
- Missing chunks.jsonl yields a structured failure (no raise).
- Source_turn_ids reference only chunk_ids present in chunks.jsonl;
  unknown ids are marked source_turn_validation="invalid".
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core.extraction.typed_extraction_runner import (
    find_meeting_extraction,
    run_typed_extraction,
)


def _seed_source(
    store_root: Path,
    source_id: str,
    chunks: list[dict],
    family: str = "meetings",
    source_artifact_id: str = "33333333-3333-3333-3333-333333333333",
) -> Path:
    processed = store_root / "processed" / family / source_id
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "source_record.json").write_text(
        json.dumps({
            "artifact_kind": "source_record",
            "artifact_type": "source_record",
            "artifact_id": source_artifact_id,
            "payload": {
                "source_id": source_id, "source_family": family,
                "title": "T", "source_type": "txt",
                "raw_path": "x", "raw_hash": "sha256:" + "0" * 64,
                "text_unit_count": len(chunks),
                "processed_path": str(processed),
                "metadata": {},
            },
        }),
        encoding="utf-8",
    )
    stories = processed / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    with (stories / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")
    return processed


def _classifier_router(text_to_classification: dict):
    """Build a stub classifier api_caller that classifies based on whether
    a substring of the chunk text is in the prompt.

    Pass {"approve plan A": "decision", "send working paper": "action_item"}
    etc. First match wins.
    """
    def caller(prompt: str) -> dict:
        for needle, cls in text_to_classification.items():
            if needle in prompt:
                return {"classification": cls, "confidence": 0.9}
        return {"classification": "off_topic", "confidence": 0.1}
    return caller


def _decision_caller(turn_id: str):
    def caller(_prompt: str) -> dict:
        return {"items": [{
            "decision_text": "Approve plan A",
            "decision_type": "approved",
            "stakeholders": ["NTIA"],
            "rationale": None,
            "source_turn_ids": [turn_id],
        }]}
    return caller


def _empty_caller(_prompt: str) -> dict:
    return {"items": []}


class TypedExtractionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_end_to_end_writes_meeting_extraction(self) -> None:
        chunks = [
            {"chunk_id": "c1", "text": "We approve plan A.", "source_id": "m1"},
            {"chunk_id": "c2", "text": "Action: send working paper to TIG.",
             "source_id": "m1"},
        ]
        _seed_source(self.store_root, "m1", chunks)

        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_router({
                        "approve plan A": "decision",
                        "send working paper": "action_item",
                    }),
                    "decision": _decision_caller("c1"),
                    "claim": _empty_caller,
                    "action_item": lambda _p: {"items": [{
                        "action": "send working paper to TIG",
                        "owner": "Alice", "due": None,
                        "source_turn_ids": ["c2"],
                    }]},
                },
            )

        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertEqual(result["decisions"], 1)
        self.assertEqual(result["action_items"], 1)
        self.assertTrue(Path(result["path"]).is_file())

        # Idempotency: second run without force => skipped.
        with mock.patch.dict(os.environ, env, clear=False):
            second = run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_router({
                        "approve plan A": "decision",
                        "send working paper": "action_item",
                    }),
                    "decision": _decision_caller("c1"),
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )
        self.assertEqual(second["status"], "skipped")

        # Force re-runs and overwrites.
        with mock.patch.dict(os.environ, env, clear=False):
            third = run_typed_extraction(
                "m1", force=True,
                api_callers={
                    "classifier": _classifier_router({
                        "approve plan A": "decision",
                        "send working paper": "action_item",
                    }),
                    "decision": _decision_caller("c1"),
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )
        self.assertEqual(third["status"], "success")

    def test_missing_chunks_jsonl_returns_failure(self) -> None:
        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction("does-not-exist")
        self.assertEqual(result["status"], "failure")
        self.assertIn("chunks_jsonl_not_found", result["reason"])

    def test_unknown_source_turn_id_marked_invalid(self) -> None:
        chunks = [
            {"chunk_id": "c1", "text": "We approve plan A.", "source_id": "m1"},
        ]
        _seed_source(self.store_root, "m1", chunks)

        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_router({
                        "approve plan A": "decision",
                    }),
                    "decision": _decision_caller("c999"),  # not in chunks
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )
        self.assertEqual(result["status"], "success")

        # Read the artifact and verify the decision is marked invalid
        artifact_path = Path(result["path"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(len(artifact["decisions"]), 1)
        self.assertEqual(
            artifact["decisions"][0]["source_turn_validation"], "invalid",
        )

    def test_find_meeting_extraction_round_trip(self) -> None:
        chunks = [{"chunk_id": "c1", "text": "x", "source_id": "m1"}]
        _seed_source(
            self.store_root, "m1", chunks,
            source_artifact_id="44444444-4444-4444-4444-444444444444",
        )
        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_router({}),
                    "decision": _empty_caller,
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )
            path = find_meeting_extraction(
                "44444444-4444-4444-4444-444444444444",
                data_lake=str(self.data_lake),
            )
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
