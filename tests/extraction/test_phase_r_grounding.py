"""Phase R.3: post-hoc source verification (token overlap) tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction.source_grounding_verifier import (
    SOURCE_GROUNDING_OVERLAP_THRESHOLD,
    SPURIOUS_ADD_RATE_THRESHOLD,
    compute_token_overlap,
    post_hoc_verification_enabled,
    verify_extraction_grounding,
    verify_source_grounding,
    write_grounding_artifacts,
)
from spectrum_systems_core.validation import validate_artifact


@contextmanager
def _env(**vars: str | None) -> Iterator[None]:
    prev: dict[str, str | None] = {}
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


def _chunk(chunk_id: str, text: str) -> dict[str, Any]:
    return {"chunk_id": chunk_id, "text": text}


class TokenOverlapTests(unittest.TestCase):
    def test_full_overlap_is_one(self) -> None:
        ov = compute_token_overlap("hello world", "hello world")
        self.assertEqual(ov, 1.0)

    def test_zero_overlap_is_zero(self) -> None:
        ov = compute_token_overlap("apple banana", "carrot date")
        self.assertEqual(ov, 0.0)

    def test_fifty_percent_overlap(self) -> None:
        # Two evidence tokens, one present in chunk.
        ov = compute_token_overlap("apple banana", "apple zebra zebra zebra")
        self.assertEqual(ov, 0.5)

    def test_empty_evidence_scores_zero(self) -> None:
        # Empty evidence must never score 100% (would be a silent
        # "no-evidence -> verified" hole).
        ov = compute_token_overlap("", "anything")
        self.assertEqual(ov, 0.0)

    def test_case_insensitive(self) -> None:
        ov = compute_token_overlap("FCC Approved", "fcc approved coordination")
        self.assertEqual(ov, 1.0)


class VerifySourceGroundingTests(unittest.TestCase):
    def test_grounded_when_overlap_above_threshold(self) -> None:
        item = {
            "candidate_evidence": "FCC approved coordination",
            "source_turn_ids": ["c-1"],
        }
        chunks = {"c-1": _chunk(
            "c-1", "FCC approved coordination procedures for 12.7 GHz.",
        )}
        result = verify_source_grounding(item, chunks)
        self.assertTrue(result["grounded"])
        self.assertGreaterEqual(result["overlap"], 0.5)

    def test_not_grounded_below_threshold(self) -> None:
        item = {
            "candidate_evidence": "moon cheese saturn alpha bravo",
            "source_turn_ids": ["c-1"],
        }
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination.")}
        result = verify_source_grounding(item, chunks)
        self.assertFalse(result["grounded"])
        # Below 0.4 threshold.
        self.assertLess(result["overlap"], SOURCE_GROUNDING_OVERLAP_THRESHOLD)

    def test_nonexistent_chunk_id_is_missing(self) -> None:
        item = {
            "candidate_evidence": "something",
            "source_turn_ids": ["does-not-exist"],
        }
        result = verify_source_grounding(item, {})
        self.assertFalse(result["grounded"])
        self.assertIn("does-not-exist", result["missing_chunk_ids"])
        self.assertEqual(result["overlap"], 0.0)

    def test_alternate_source_turns_field_accepted(self) -> None:
        item = {
            "candidate_evidence": "approved",
            "source_turns": ["c-1"],  # alternate spelling
        }
        chunks = {"c-1": _chunk("c-1", "approved coordination procedures")}
        result = verify_source_grounding(item, chunks)
        self.assertTrue(result["grounded"])


class VerifyExtractionGroundingTests(unittest.TestCase):
    def test_grounded_field_present_on_annotated_items(self) -> None:
        items = [
            {"decision_text": "FCC approved coordination",
             "candidate_evidence": "FCC approved coordination",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination procedures")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertEqual(len(out["annotated_items"]), 1)
        self.assertIn("grounded", out["annotated_items"][0])
        self.assertTrue(out["annotated_items"][0]["grounded"])

    def test_spurious_emitted_for_ungrounded(self) -> None:
        items = [
            {"decision_text": "Moon cheese decision",
             "candidate_evidence": "saturn alpha bravo delta echo foxtrot",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertEqual(len(out["spurious_add_candidates"]), 1)
        validate_artifact(
            out["spurious_add_candidates"][0], "spurious_add_candidate",
        )

    def test_spurious_add_rate_computed_across_all_items(self) -> None:
        # 2 of 4 confirmed items are ungrounded → rate = 0.5.
        items = [
            {"decision_text": "good 1",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "good 2",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "bad 1",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "bad 2",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination procedures")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertEqual(out["confirmed_items_count"], 4)
        self.assertEqual(out["spurious_items_count"], 2)
        self.assertAlmostEqual(out["spurious_add_rate"], 0.5)

    def test_spurious_add_warning_fires_above_threshold(self) -> None:
        # 4/4 ungrounded → rate = 1.0 > 0.30 threshold → warning fires.
        items = [
            {"decision_text": f"bad {i}",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]}
            for i in range(4)
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertIsNotNone(out["spurious_add_warning"])
        validate_artifact(out["spurious_add_warning"], "spurious_add_warning")
        self.assertGreater(
            out["spurious_add_rate"], SPURIOUS_ADD_RATE_THRESHOLD,
        )

    def test_spurious_add_warning_does_not_fire_below_threshold(self) -> None:
        # 1/4 ungrounded → rate = 0.25 < 0.30 threshold → no warning.
        items = [
            {"decision_text": "good",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "good",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "good",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "bad",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertIsNone(out["spurious_add_warning"])

    def test_denominator_is_confirmed_only(self) -> None:
        # The caller passes only CONFIRMED items into
        # verify_extraction_grounding -- rejected items are excluded
        # upstream. We verify here that the denominator matches the
        # length of confirmed_items input only (no hidden boost).
        items = [
            {"decision_text": "good",
             "candidate_evidence": "FCC approved",
             "source_turn_ids": ["c-1"]},
            {"decision_text": "bad",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        self.assertEqual(out["confirmed_items_count"], 2)
        # Denominator strictly the count of items passed in.
        expected_rate = out["spurious_items_count"] / 2.0
        self.assertAlmostEqual(out["spurious_add_rate"], expected_rate)

    def test_artifacts_written_to_disk(self) -> None:
        items = [
            {"decision_text": "bad",
             "candidate_evidence": "saturn alpha bravo delta echo",
             "source_turn_ids": ["c-1"]},
        ]
        chunks = {"c-1": _chunk("c-1", "FCC approved coordination")}
        out = verify_extraction_grounding(
            items, chunks, source_id="s-001",
        )
        with tempfile.TemporaryDirectory() as td:
            paths = write_grounding_artifacts(out, Path(td))
            self.assertEqual(len(paths["candidates"]), 1)


class FlagTests(unittest.TestCase):
    def test_default_enabled(self) -> None:
        with _env(POST_HOC_VERIFICATION_ENABLED=None):
            self.assertTrue(post_hoc_verification_enabled())

    def test_disabled_via_env(self) -> None:
        with _env(POST_HOC_VERIFICATION_ENABLED="false"):
            self.assertFalse(post_hoc_verification_enabled())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
