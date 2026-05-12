"""Phase R.1: two-stage extraction tests."""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from spectrum_systems_core.extraction.two_stage_extractor import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    TWO_STAGE_EXTRACTION_ENABLED_ENV,
    build_candidate_prompt_block,
    build_normalize_prompt,
    normalize_candidates,
    parse_normalize_response,
    two_stage_enabled,
)


@contextmanager
def _env(**vars: Optional[str]) -> Iterator[None]:
    prev: Dict[str, Optional[str]] = {}
    for k, v in vars.items():
        prev[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, p in prev.items():
            if p is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = p


class MockNormalizeCaller:
    """Capture the prompts sent and return scripted responses."""

    def __init__(self, response: Dict[str, Any]) -> None:
        self.response = response
        self.calls: List[str] = []

    def __call__(self, prompt: str) -> Dict[str, Any]:
        self.calls.append(prompt)
        return self.response


class TwoStageFlagTests(unittest.TestCase):
    def test_default_enabled(self) -> None:
        with _env(TWO_STAGE_EXTRACTION_ENABLED=None):
            self.assertTrue(two_stage_enabled())

    def test_disabled_via_env(self) -> None:
        with _env(TWO_STAGE_EXTRACTION_ENABLED="false"):
            self.assertFalse(two_stage_enabled())
        with _env(TWO_STAGE_EXTRACTION_ENABLED="0"):
            self.assertFalse(two_stage_enabled())

    def test_bypass_returns_all_as_confirmed(self) -> None:
        cands = [
            {"text": "FCC approved coordination",
             "candidate_evidence": "FCC approved coordination procedures."},
        ]
        with _env(TWO_STAGE_EXTRACTION_ENABLED="false"):
            confirmed, rejected = normalize_candidates(
                cands, "FCC approved coordination procedures.",
                api_caller=None,
            )
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["normalize_status"], STATUS_CONFIRMED)
        self.assertTrue(confirmed[0].get("normalize_bypass"))
        self.assertEqual(rejected, [])


class PromptShapeTests(unittest.TestCase):
    def test_candidate_prompt_block_requires_candidate_evidence(self) -> None:
        block = build_candidate_prompt_block()
        self.assertIn("candidate_evidence", block)
        self.assertIn("do not emit the item", block)

    def test_normalize_prompt_includes_chunk_and_candidates(self) -> None:
        cands = [
            {"text": "A decision was made", "candidate_evidence": "made"},
        ]
        chunk = "FCC approved 12.7 GHz coordination procedures."
        prompt = build_normalize_prompt(cands, chunk)
        self.assertIn(chunk, prompt)
        self.assertIn("candidate_index", prompt)
        self.assertIn("Confirm or reject only", prompt)


class ParseNormalizeResponseTests(unittest.TestCase):
    def test_confirmed_round_trip(self) -> None:
        resp = {"normalized": [
            {"candidate_index": 0, "status": "confirmed"},
            {"candidate_index": 1, "status": "rejected",
             "rejection_reason": "not in source"},
        ]}
        decisions = parse_normalize_response(resp, candidate_count=2)
        self.assertEqual(decisions[0]["status"], STATUS_CONFIRMED)
        self.assertEqual(decisions[1]["status"], STATUS_REJECTED)
        self.assertIn("not in source", decisions[1]["rejection_reason"])

    def test_missing_entries_default_to_rejected(self) -> None:
        # Only one entry in the response, but there are two candidates:
        # the missing slot must default to rejected (fail-closed).
        resp = {"normalized": [{"candidate_index": 0, "status": "confirmed"}]}
        decisions = parse_normalize_response(resp, candidate_count=2)
        self.assertEqual(decisions[0]["status"], STATUS_CONFIRMED)
        self.assertEqual(decisions[1]["status"], STATUS_REJECTED)

    def test_unrecognised_status_defaults_to_rejected(self) -> None:
        resp = {"normalized": [{"candidate_index": 0, "status": "maybe"}]}
        decisions = parse_normalize_response(resp, candidate_count=1)
        self.assertEqual(decisions[0]["status"], STATUS_REJECTED)
        self.assertIn("unrecognised_status", decisions[0]["rejection_reason"])

    def test_out_of_range_index_ignored(self) -> None:
        resp = {"normalized": [{"candidate_index": 99, "status": "confirmed"}]}
        decisions = parse_normalize_response(resp, candidate_count=1)
        # Slot 0 was not addressed -> defaults to rejected.
        self.assertEqual(decisions[0]["status"], STATUS_REJECTED)


class NormalizeCandidatesTests(unittest.TestCase):
    def test_stage_two_confirms_with_valid_evidence(self) -> None:
        cands = [
            {"text": "FCC approved coordination",
             "candidate_evidence": "FCC approved coordination procedures."},
        ]
        caller = MockNormalizeCaller({"normalized": [
            {"candidate_index": 0, "status": "confirmed"},
        ]})
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "FCC approved coordination procedures.",
                api_caller=caller,
            )
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["normalize_status"], STATUS_CONFIRMED)
        self.assertEqual(rejected, [])
        # The model was called exactly once for the chunk.
        self.assertEqual(len(caller.calls), 1)

    def test_stage_two_rejects_when_evidence_absent(self) -> None:
        cands = [
            {"text": "FCC banned all radios",
             "candidate_evidence": "FCC prohibited every transmitter."},
        ]
        # Source text does not contain the evidence; the mock model
        # rejects.
        caller = MockNormalizeCaller({"normalized": [
            {"candidate_index": 0, "status": "rejected",
             "rejection_reason": "evidence_absent"},
        ]})
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "Meeting opened; roll call taken.",
                api_caller=caller,
            )
        self.assertEqual(confirmed, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["normalize_status"], STATUS_REJECTED)
        self.assertIn("evidence_absent", rejected[0]["rejection_reason"])

    def test_confirmed_only_in_canonical_output(self) -> None:
        # Mixed batch: confirm one, reject one. Only the confirmed item
        # makes it into the confirmed list.
        cands = [
            {"text": "FCC approved", "candidate_evidence": "FCC approved"},
            {"text": "FCC denied", "candidate_evidence": "denied everything"},
        ]
        caller = MockNormalizeCaller({"normalized": [
            {"candidate_index": 0, "status": "confirmed"},
            {"candidate_index": 1, "status": "rejected",
             "rejection_reason": "absent"},
        ]})
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "FCC approved procedures.", api_caller=caller,
            )
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["text"], "FCC approved")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["text"], "FCC denied")

    def test_two_model_calls_per_chunk(self) -> None:
        # Stage 1 is invoked by the existing extractor (not in this
        # module's scope). Stage 2 is invoked by ``normalize_candidates``.
        # Together: exactly two model calls per chunk that produced at
        # least one candidate. We verify the stage-2 side here.
        cands = [{"text": "T", "candidate_evidence": "T"}]
        caller = MockNormalizeCaller({"normalized": [
            {"candidate_index": 0, "status": "confirmed"},
        ]})
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            normalize_candidates(cands, "T", api_caller=caller)
        # One stage-2 call (the second model call per chunk).
        self.assertEqual(len(caller.calls), 1)

    def test_fail_closed_when_no_api_caller(self) -> None:
        # When two-stage is on but no api_caller is injected the
        # normalize step rejects every candidate. Auto-confirming with
        # no model running would be a silent collapse to single-stage.
        cands = [{"text": "T", "candidate_evidence": "T"}]
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "T", api_caller=None,
            )
        self.assertEqual(confirmed, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["rejection_reason"], "no_normalize_api_caller")

    def test_fail_closed_on_api_exception(self) -> None:
        def boom(prompt: str) -> Dict[str, Any]:
            raise RuntimeError("network down")
        cands = [{"text": "T", "candidate_evidence": "T"}]
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "T", api_caller=boom,
            )
        self.assertEqual(confirmed, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("normalize_api_error", rejected[0]["rejection_reason"])

    def test_all_rejected_returns_empty_confirmed(self) -> None:
        # When the model rejects every candidate the confirmed list is
        # empty -- the runner is expected to interpret this as an
        # "all rejected" outcome and emit the empty_result failure
        # artifact (tested separately in the runner tests).
        cands = [
            {"text": "A", "candidate_evidence": "evidence A"},
            {"text": "B", "candidate_evidence": "evidence B"},
        ]
        caller = MockNormalizeCaller({"normalized": [
            {"candidate_index": 0, "status": "rejected",
             "rejection_reason": "x"},
            {"candidate_index": 1, "status": "rejected",
             "rejection_reason": "x"},
        ]})
        with _env(TWO_STAGE_EXTRACTION_ENABLED="true"):
            confirmed, rejected = normalize_candidates(
                cands, "irrelevant chunk text", api_caller=caller,
            )
        self.assertEqual(confirmed, [])
        self.assertEqual(len(rejected), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
