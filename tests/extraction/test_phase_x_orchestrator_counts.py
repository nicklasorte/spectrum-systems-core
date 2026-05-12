"""Phase X-0 part D integration test: orchestration counters land in the
artifact, failure artifacts are emitted, and the stage_status reflects
the chunks_blocked / chunks_attempted ratio.

These tests drive ``run_typed_extraction`` end-to-end with stub LLM
callers that inject the relevant failure modes (rate limit exhaustion,
empty response, parse error, zero items).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anthropic

from spectrum_systems_core.extraction.typed_extraction_runner import (
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
            "payload": {"source_id": source_id, "source_family": family},
        }),
        encoding="utf-8",
    )
    stories = processed / "stories"
    stories.mkdir(parents=True, exist_ok=True)
    with (stories / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")
    return processed


def _classifier_decision_for(text_match: str):
    def caller(prompt: str) -> dict:
        # Batch caller signature returns {"text": ...} with one
        # `chunk_id: ... | classification: ...` line per chunk.
        # The runner sniffs all chunks for the substring.
        lines = []
        # Build a permissive batch reply that classifies every chunk
        # whose text contains text_match as "decision".
        # Each batch prompt enumerates chunks with their chunk_ids.
        import re
        for m in re.finditer(
            r"Chunk \d+ \(chunk_id: ([^)]+)\):\n(.+)", prompt,
        ):
            cid = m.group(1).strip()
            body = m.group(2)
            cls = "decision" if text_match in body else "off_topic"
            lines.append(
                f"chunk_id: {cid} | classification: {cls}"
            )
        return {"text": "\n".join(lines)}
    return caller


def _decision_extract_caller(turn_id: str):
    def caller(_prompt: str) -> dict:
        return {
            "items": [
                {
                    "decision_text": "approved",
                    "decision_type": "approved",
                    "stakeholders": [],
                    "rationale": None,
                    "source_turn_ids": [turn_id],
                    "confidence": 0.9,
                }
            ]
        }
    return caller


def _empty_caller(_p):
    return {"items": []}


def _make_rate_limit_error() -> anthropic.RateLimitError:
    exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    Exception.__init__(exc, "rate_limit")
    return exc


class OrchestratorCountsHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success_writes_orchestration_result_with_zero_blocked(self) -> None:
        chunks = [
            {"chunk_id": "c1", "text": "We approve plan A.", "source_id": "m1"},
            {"chunk_id": "c2", "text": "We approve plan B.", "source_id": "m1"},
        ]
        _seed_source(self.store_root, "m1", chunks)

        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_decision_for("approve"),
                    "decision": _decision_extract_caller("c1"),
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )

        self.assertEqual(result["status"], "success", msg=result.get("reason"))
        self.assertEqual(result["chunks_attempted"], 2)
        self.assertEqual(result["chunks_blocked"], 0)
        self.assertEqual(result["stage_status"], "ok")
        # Orchestration artifact landed under sdl_root/orchestration/.
        orch_path = result["orchestration_result_path"]
        self.assertTrue(orch_path)
        doc = json.loads(Path(orch_path).read_text())
        self.assertEqual(doc["artifact_type"], "orchestration_result")
        self.assertEqual(doc["chunks_attempted"], 2)
        self.assertEqual(doc["chunks_blocked"], 0)
        self.assertEqual(doc["stage_status"], "ok")
        # block_reasons must be present with all four keys at zero.
        self.assertEqual(
            doc["block_reasons"],
            {
                "rate_limit_exhausted": 0,
                "empty_response": 0,
                "parse_error": 0,
                "other": 0,
            },
        )

    def test_zero_items_emits_empty_result_artifact(self) -> None:
        chunks = [
            {"chunk_id": "c1", "text": "totally off topic", "source_id": "m1"},
        ]
        _seed_source(self.store_root, "m1", chunks)

        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                "m1",
                api_callers={
                    "classifier": _classifier_decision_for("approve"),
                    "decision": _empty_caller,
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )

        self.assertEqual(result["status"], "success")
        # No items -> the empty_result failure artifact bumped `other`.
        self.assertEqual(result["block_reasons"]["other"], 1)
        # Failure artifact landed under sdl_root/failures/.
        sdl_root = self.store_root / "artifacts"
        failures = list((sdl_root / "failures").glob("*.json"))
        empty_result_failures = [
            f for f in failures
            if json.loads(f.read_text())["artifact_type"]
            == "typed_extraction_empty_result"
        ]
        self.assertEqual(len(empty_result_failures), 1)


class OrchestratorCountsRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rate_limit_blocks_all_chunks_and_emits_artifact(self) -> None:
        chunks = [
            {"chunk_id": f"c{i}", "text": "approve", "source_id": "m1"}
            for i in range(3)
        ]
        _seed_source(self.store_root, "m1", chunks)

        async def rate_limited_async_caller(_prompt):
            raise _make_rate_limit_error()

        env = {"DATA_LAKE_PATH": str(self.data_lake), "ANTHROPIC_API_KEY": "sk"}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction(
                "m1",
                async_classifier_caller=rate_limited_async_caller,
                api_callers={
                    "classifier": _classifier_decision_for("approve"),
                    "decision": _empty_caller,
                    "claim": _empty_caller,
                    "action_item": _empty_caller,
                },
            )

        self.assertEqual(result["status"], "failure")
        self.assertIn("api_rate_limit_exhausted", result["reason"])
        self.assertEqual(result["chunks_attempted"], 3)
        # All 3 chunks counted as blocked under rate_limit_exhausted.
        self.assertEqual(result["chunks_blocked"], 3)
        self.assertEqual(
            result["block_reasons"]["rate_limit_exhausted"], 3
        )
        # Majority blocked => stage_status=failed.
        self.assertEqual(result["stage_status"], "failed")
        # api_rate_limit_exhausted failure artifacts under failures/.
        sdl_root = self.store_root / "artifacts"
        failures = list((sdl_root / "failures").glob("*.json"))
        rl_failures = [
            f for f in failures
            if json.loads(f.read_text())["artifact_type"]
            == "api_rate_limit_exhausted"
        ]
        self.assertEqual(len(rl_failures), 3)


if __name__ == "__main__":
    unittest.main()
