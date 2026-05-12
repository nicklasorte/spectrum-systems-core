"""Tests for batch + async classification on ChunkClassifier.

Phase Perf. Verifies:
- batch_classify returns one artifact per chunk in input order,
- malformed lines fall back to off_topic,
- regulatory-verb fallback is applied per chunk after batch parse,
- API errors fall back to per-chunk classify(),
- BATCH_SIZE caps the number of API calls per run,
- batch_classify_async preserves order with a Semaphore-bounded fan-out.
"""
from __future__ import annotations

import asyncio
import math
import re
import unittest
from typing import Any, Dict, List
from unittest import mock

from spectrum_systems_core.extraction.chunk_classifier import ChunkClassifier


def _make_chunks(n: int, prefix: str = "c", text_for=None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in range(n):
        cid = f"{prefix}{i}"
        text = text_for(i) if text_for else f"speaker turn body {i}"
        out.append({"chunk_id": cid, "text": text})
    return out


def _batch_response_for(chunks, classification: str = "off_topic") -> str:
    return "\n".join(
        f"chunk_id: {c['chunk_id']} | classification: {classification}"
        for c in chunks
    )


class BatchClassifyReturnsOnePerChunkTests(unittest.TestCase):
    def test_returns_one_artifact_per_input_chunk(self) -> None:
        chunks = _make_chunks(10)
        captured = {}

        def caller(prompt: str) -> Dict[str, Any]:
            captured["prompt"] = prompt
            return {"text": _batch_response_for(chunks, "claim")}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(len(results), 10)
        for c, r in zip(chunks, results):
            self.assertEqual(r["chunk_id"], c["chunk_id"])
            self.assertEqual(r["classification"], "claim")
            self.assertEqual(r["source_id"], "mtg-001")
            self.assertEqual(r["artifact_type"], "chunk_classification")

    def test_preserves_chunk_order(self) -> None:
        chunks = _make_chunks(5)

        def caller(prompt: str) -> Dict[str, Any]:
            # Deliberately scramble the order in the response.
            scrambled = list(reversed(chunks))
            return {"text": _batch_response_for(scrambled, "claim")}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        # Even though the response was reversed, the artifact order must
        # match the input order of chunks.
        for c, r in zip(chunks, results):
            self.assertEqual(r["chunk_id"], c["chunk_id"])

    def test_empty_input_returns_empty_list_no_api_call(self) -> None:
        calls: List[str] = []

        def caller(prompt: str) -> Dict[str, Any]:
            calls.append(prompt)
            return {"text": ""}

        clf = ChunkClassifier(api_caller=caller)
        self.assertEqual(clf.batch_classify([], source_id="mtg-001"), [])
        self.assertEqual(calls, [])


class BatchClassifyMalformedResponseTests(unittest.TestCase):
    def test_malformed_line_gets_off_topic(self) -> None:
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            # c0 well-formed, c1 broken, c2 well-formed
            return {
                "text": (
                    "chunk_id: c0 | classification: claim\n"
                    "garbage line that does not parse\n"
                    "chunk_id: c2 | classification: action_item\n"
                )
            }

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(results[0]["classification"], "claim")
        # The missing chunk -- its line is garbage -- specifically becomes
        # off_topic (not a crash, not silently dropped).
        self.assertEqual(results[1]["classification"], "off_topic")
        self.assertEqual(results[1]["chunk_id"], "c1")
        self.assertEqual(results[2]["classification"], "action_item")

    def test_missing_chunk_lines_get_off_topic_not_indexerror(self) -> None:
        # LLM only emits a line for c0; c1, c2 must not raise IndexError
        # -- they get off_topic instead.
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            return {"text": "chunk_id: c0 | classification: decision\n"}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["classification"], "decision")
        self.assertEqual(results[1]["classification"], "off_topic")
        self.assertEqual(results[2]["classification"], "off_topic")


class BatchRegulatoryVerbFallbackTests(unittest.TestCase):
    def test_off_topic_with_approved_promoted_to_decision(self) -> None:
        chunks = [{"chunk_id": "c0", "text": "The motion was approved."}]

        def caller(prompt: str) -> Dict[str, Any]:
            return {"text": "chunk_id: c0 | classification: off_topic\n"}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(results[0]["classification"], "decision")
        self.assertTrue(results[0]["regulatory_verb_fallback_applied"])

    def test_off_topic_without_verb_stays_off_topic(self) -> None:
        chunks = [{"chunk_id": "c0", "text": "Just discussing the weather."}]

        def caller(prompt: str) -> Dict[str, Any]:
            return {"text": "chunk_id: c0 | classification: off_topic\n"}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(results[0]["classification"], "off_topic")
        self.assertFalse(results[0]["regulatory_verb_fallback_applied"])


class BatchApiErrorFallbackTests(unittest.TestCase):
    def test_api_error_falls_back_to_per_chunk_classify(self) -> None:
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            raise RuntimeError("network down")

        clf = ChunkClassifier(api_caller=caller)
        # Spy on classify() to verify per-chunk fallback was actually invoked.
        with mock.patch.object(
            clf, "classify", wraps=clf.classify,
        ) as classify_spy:
            results = clf.batch_classify(chunks, source_id="mtg-001")

        # Per-chunk classify() called once per chunk on error.
        self.assertEqual(classify_spy.call_count, 3)
        # Results still returned (not empty, not crashed).
        self.assertEqual(len(results), 3)
        # Each result is still a valid classification artifact.
        for r in results:
            self.assertIn(r["classification"], (
                "decision", "claim", "action_item", "off_topic",
            ))

    def test_unexpected_response_shape_falls_back_to_per_chunk(self) -> None:
        # Caller returns a per-chunk-style dict (e.g. legacy injected
        # caller that wasn't updated to the batch contract). Must NOT
        # silently classify everything off_topic; must fall back to
        # per-chunk classify().
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            return {"classification": "decision"}

        clf = ChunkClassifier(api_caller=caller)
        with mock.patch.object(
            clf, "classify", wraps=clf.classify,
        ) as classify_spy:
            results = clf.batch_classify(chunks, source_id="mtg-001")
        self.assertEqual(classify_spy.call_count, 3)
        self.assertEqual(len(results), 3)


class BatchHallucinatedAndDuplicateChunkIdTests(unittest.TestCase):
    def test_hallucinated_chunk_id_triggers_per_chunk_fallback(self) -> None:
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            return {
                "text": (
                    "chunk_id: c0 | classification: claim\n"
                    # NOT_REQUESTED was never sent in the batch.
                    "chunk_id: NOT_REQUESTED | classification: decision\n"
                    "chunk_id: c2 | classification: action_item\n"
                )
            }

        clf = ChunkClassifier(api_caller=caller)
        with mock.patch.object(
            clf, "classify", wraps=clf.classify,
        ) as classify_spy:
            results = clf.batch_classify(chunks, source_id="mtg-001")

        # Hallucinated id forces per-chunk re-classification of the whole
        # batch -- we cannot trust the response.
        self.assertEqual(classify_spy.call_count, 3)
        self.assertEqual(len(results), 3)

    def test_duplicate_chunk_id_triggers_per_chunk_fallback(self) -> None:
        chunks = _make_chunks(3)

        def caller(prompt: str) -> Dict[str, Any]:
            # c0 emitted twice with conflicting classifications. Without
            # the duplicate guard the second would silently win; with it
            # we re-classify the whole batch per-chunk.
            return {
                "text": (
                    "chunk_id: c0 | classification: decision\n"
                    "chunk_id: c0 | classification: off_topic\n"
                    "chunk_id: c1 | classification: claim\n"
                    "chunk_id: c2 | classification: action_item\n"
                )
            }

        clf = ChunkClassifier(api_caller=caller)
        with mock.patch.object(
            clf, "classify", wraps=clf.classify,
        ) as classify_spy:
            results = clf.batch_classify(chunks, source_id="mtg-001")

        self.assertEqual(classify_spy.call_count, 3)
        self.assertEqual(len(results), 3)


class BatchSizeCappingTests(unittest.TestCase):
    def test_batch_size_limits_chunks_per_call(self) -> None:
        chunks = _make_chunks(200)
        call_count = {"n": 0}

        def caller(prompt: str) -> Dict[str, Any]:
            call_count["n"] += 1
            # Honour the batch contract: parse the chunk_ids out of the
            # prompt and emit one classification line per id that the
            # batch actually requested. Emitting ids beyond the batch
            # would (correctly) trigger the hallucination guard.
            ids_in_prompt = re.findall(r"\(chunk_id:\s*(c\d+)\)", prompt)
            lines = "\n".join(
                f"chunk_id: {cid} | classification: claim"
                for cid in ids_in_prompt
            )
            return {"text": lines}

        clf = ChunkClassifier(api_caller=caller)
        results = clf.batch_classify(chunks, source_id="mtg-001")

        expected = math.ceil(200 / ChunkClassifier.BATCH_SIZE)
        self.assertEqual(call_count["n"], expected)
        self.assertEqual(len(results), 200)
        # All results came from the batch path (claim), not per-chunk
        # fallback (which would be off_topic).
        self.assertTrue(all(r["classification"] == "claim" for r in results))

    def test_num_batches_for_helper(self) -> None:
        self.assertEqual(ChunkClassifier().num_batches_for(0), 0)
        self.assertEqual(ChunkClassifier().num_batches_for(1), 1)
        self.assertEqual(
            ChunkClassifier().num_batches_for(ChunkClassifier.BATCH_SIZE),
            1,
        )
        self.assertEqual(
            ChunkClassifier().num_batches_for(ChunkClassifier.BATCH_SIZE + 1),
            2,
        )


class BatchClassifyAsyncTests(unittest.TestCase):
    def test_async_batch_preserves_chunk_order(self) -> None:
        chunks = _make_chunks(40)

        async def acaller(prompt: str) -> Dict[str, Any]:
            # Honour the batch contract: emit lines only for the
            # chunk_ids actually present in this batch's prompt. The
            # parser correctly rejects hallucinated chunk_ids, so a
            # global "0..N" emit would trigger the per-chunk fallback.
            ids_in_prompt = re.findall(r"\(chunk_id:\s*(c\d+)\)", prompt)
            lines = "\n".join(
                f"chunk_id: {cid} | classification: claim"
                for cid in ids_in_prompt
            )
            await asyncio.sleep(0)
            return {"text": lines}

        clf = ChunkClassifier()
        results = asyncio.run(
            clf.batch_classify_async(
                chunks,
                source_id="mtg-001",
                async_caller=acaller,
                max_concurrent=3,
            )
        )

        self.assertEqual(len(results), 40)
        for c, r in zip(chunks, results):
            self.assertEqual(r["chunk_id"], c["chunk_id"])
            self.assertEqual(r["classification"], "claim")

    def test_async_max_concurrent_respected(self) -> None:
        # Track max in-flight concurrency to verify the Semaphore cap.
        chunks = _make_chunks(60)
        in_flight = {"current": 0, "peak": 0}

        async def acaller(prompt: str) -> Dict[str, Any]:
            in_flight["current"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["current"])
            try:
                await asyncio.sleep(0.01)
                ids_in_prompt = re.findall(r"\(chunk_id:\s*(c\d+)\)", prompt)
                lines = "\n".join(
                    f"chunk_id: {cid} | classification: claim"
                    for cid in ids_in_prompt
                )
                return {"text": lines}
            finally:
                in_flight["current"] -= 1

        clf = ChunkClassifier()
        asyncio.run(
            clf.batch_classify_async(
                chunks,
                source_id="mtg-001",
                async_caller=acaller,
                max_concurrent=3,
            )
        )

        # Peak concurrency must NEVER exceed max_concurrent.
        self.assertLessEqual(in_flight["peak"], 3)
        # And it should reach the cap (we have ceil(60/15)=4 batches, > 3).
        self.assertEqual(in_flight["peak"], 3)

    def test_async_falls_back_per_chunk_on_error(self) -> None:
        chunks = _make_chunks(3)

        async def acaller(prompt: str) -> Dict[str, Any]:
            raise RuntimeError("network down")

        clf = ChunkClassifier()
        with mock.patch.object(
            clf, "classify", wraps=clf.classify,
        ) as classify_spy:
            results = asyncio.run(
                clf.batch_classify_async(
                    chunks,
                    source_id="mtg-001",
                    async_caller=acaller,
                    max_concurrent=2,
                )
            )

        self.assertEqual(classify_spy.call_count, 3)
        self.assertEqual(len(results), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
