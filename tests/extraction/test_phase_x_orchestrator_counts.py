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


class SpuriousAddCountHelperTests(unittest.TestCase):
    """Phase Z.4: unit coverage for the pure derivation helper.

    The helper reads the EXISTING verification summary; it must never
    re-measure or invent a value. None / malformed summary -> 0 (the
    honest "no verifier ran" value, not a silent skip).
    """

    def test_none_verification_result_is_zero(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        self.assertEqual(_spurious_add_count_from_verification(None), 0)

    def test_missing_or_malformed_summary_is_zero(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        self.assertEqual(_spurious_add_count_from_verification({}), 0)
        self.assertEqual(
            _spurious_add_count_from_verification({"summary": None}), 0
        )
        self.assertEqual(
            _spurious_add_count_from_verification({"summary": "x"}), 0
        )

    def test_sums_unsupported_and_contradicted(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        summary = {
            "summary": {
                "total_items_count": 5,
                "verified_count": 2,
                "unsupported_count": 2,
                "contradicted_count": 1,
                "insufficient_evidence_count": 0,
                "verification_failed_count": 0,
                "spurious_add_rate": 0.6,
                "status": "complete",
            }
        }
        self.assertEqual(
            _spurious_add_count_from_verification(summary), 3
        )

    def test_all_verified_summary_is_zero(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        summary = {
            "summary": {
                "unsupported_count": 0,
                "contradicted_count": 0,
            }
        }
        self.assertEqual(
            _spurious_add_count_from_verification(summary), 0
        )

    def test_bool_is_not_counted_as_int(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        # bool is a subclass of int; True must not be coerced to 1.
        summary = {
            "summary": {
                "unsupported_count": True,
                "contradicted_count": 2,
            }
        }
        self.assertEqual(
            _spurious_add_count_from_verification(summary), 2
        )

    def test_helper_reads_real_verifier_summary(self) -> None:
        """Anti-duplication proof: the count is the integer numerator
        behind the REAL post_hoc_verifier ``spurious_add_rate``. Build a
        real summary via the verifier's own _compute_summary, not a
        hand-rolled dict, so a rename of the summary keys breaks this
        test instead of silently zeroing the metric."""
        from spectrum_systems_core.verification.post_hoc_verifier import (
            PostHocVerifier,
        )
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _spurious_add_count_from_verification,
        )
        verifier = PostHocVerifier.__new__(PostHocVerifier)
        item_verifications = [
            {"verification_status": "verified"},
            {"verification_status": "unsupported"},
            {"verification_status": "contradicted"},
            {"verification_status": "insufficient_evidence"},
        ]
        summary = verifier._compute_summary(
            item_verifications, halted=False
        )
        # Sanity: the rate's numerator is unsupported + contradicted = 2.
        self.assertEqual(summary["unsupported_count"], 1)
        self.assertEqual(summary["contradicted_count"], 1)
        self.assertEqual(
            _spurious_add_count_from_verification(
                {"summary": summary}
            ),
            2,
        )


def _verifier_caller(status: str):
    """Deterministic stub for the Phase V verifier api_caller. NO LLM:
    returns a fixed verification verdict for every item."""
    def caller(_prompt):
        return {
            "verification_status": status,
            "supporting_text_excerpts": (
                ["We approve plan A."] if status == "verified" else []
            ),
            "verifier_confidence": 0.9,
            "verifier_rationale": "stub",
        }
    return caller


class SpuriousAddCountOnDiskTests(unittest.TestCase):
    """Phase Z.4 synthetic regression: a run whose verifier marks the
    extracted item ``unsupported`` (the fabricated-claim case) must land
    ``spurious_add_count >= 1`` on the orchestration_result on disk —
    proving the metric is live, not structurally always-zero. The
    Phase-V-off companion proves it is always present and 0 when no
    verifier ran (the None-filter in _write_orchestration_result does
    not drop it).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.store_root = self.data_lake / "store"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enable_phase_v(self) -> None:
        from spectrum_systems_core.config.feature_flag import (
            PHASE_V_FLAG_NAME,
        )
        cfg = self.store_root / "artifacts" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8",
        )

    def _run(self, *, verifier_status: str | None):
        chunks = [
            {"chunk_id": "c1", "text": "We approve plan A.", "source_id": "m1"},
        ]
        _seed_source(self.store_root, "m1", chunks)
        callers = {
            "classifier": _classifier_decision_for("approve"),
            "decision": _decision_extract_caller("c1"),
            "claim": _empty_caller,
            "action_item": _empty_caller,
        }
        if verifier_status is not None:
            self._enable_phase_v()
            callers["verifier"] = _verifier_caller(verifier_status)
        env = {"DATA_LAKE_PATH": str(self.data_lake)}
        with mock.patch.dict(os.environ, env, clear=False):
            result = run_typed_extraction("m1", api_callers=callers)
        self.assertEqual(
            result["status"], "success", msg=result.get("reason")
        )
        orch_path = result["orchestration_result_path"]
        self.assertTrue(orch_path)
        return json.loads(Path(orch_path).read_text())

    def test_fabricated_claim_run_has_nonzero_spurious_add_count(self) -> None:
        doc = self._run(verifier_status="unsupported")
        self.assertIn("spurious_add_count", doc)
        self.assertGreaterEqual(doc["spurious_add_count"], 1)

    def test_all_verified_run_has_zero_spurious_add_count(self) -> None:
        doc = self._run(verifier_status="verified")
        self.assertIn("spurious_add_count", doc)
        self.assertEqual(doc["spurious_add_count"], 0)

    def test_phase_v_off_run_still_has_field_at_zero(self) -> None:
        # No verifier ran: the field is still present and 0 (not absent,
        # not always-nonzero). Proves the None-filter does not drop it.
        doc = self._run(verifier_status=None)
        self.assertIn("spurious_add_count", doc)
        self.assertEqual(doc["spurious_add_count"], 0)


if __name__ == "__main__":
    unittest.main()
