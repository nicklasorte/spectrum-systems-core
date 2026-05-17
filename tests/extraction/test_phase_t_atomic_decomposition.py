"""Phase T.7 tests: atomic decomposition (feature flagged off by default)."""
from __future__ import annotations

import os
import unittest
from typing import Any

from spectrum_systems_core.extraction.atomic_decomposition import (
    ATOMIC_DECOMPOSITION_ENABLED_ENV,
    atomic_decomposition_enabled,
    decompose_decisions,
    decompose_one,
)


class FeatureFlagTests(unittest.TestCase):

    def setUp(self) -> None:
        os.environ.pop(ATOMIC_DECOMPOSITION_ENABLED_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(ATOMIC_DECOMPOSITION_ENABLED_ENV, None)

    def test_disabled_by_default(self) -> None:
        self.assertFalse(atomic_decomposition_enabled())

    def test_no_calls_when_disabled(self) -> None:
        call_log: list[str] = []

        def caller(prompt: str) -> dict[str, Any]:
            call_log.append(prompt)
            return {"atomic_facts": ["x"]}

        decisions = [{"decision_text": "FCC approved band plan.", "confidence": 0.9}]
        annotated, findings, count = decompose_decisions(
            decisions, api_caller=caller, source_id="s",
        )
        # RT pass 3: with the flag off the call count MUST be zero.
        self.assertEqual(call_log, [])
        self.assertEqual(count, 0)
        self.assertEqual(annotated[0]["atomic_facts"], None)
        self.assertEqual(findings, [])


class DecomposeOneTests(unittest.TestCase):

    def test_returns_list_when_caller_succeeds(self) -> None:
        def caller(prompt: str) -> dict[str, Any]:
            return {"atomic_facts": ["FCC approved band plan A.", "Effective 2026."]}

        facts = decompose_one("FCC approved band plan A, effective 2026.", caller)
        self.assertEqual(facts, ["FCC approved band plan A.", "Effective 2026."])

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(decompose_one("", lambda p: {"atomic_facts": ["x"]}), [])

    def test_caller_raises_returns_empty(self) -> None:
        def caller(prompt: str) -> dict[str, Any]:
            raise RuntimeError("boom")

        self.assertEqual(decompose_one("decision text", caller), [])

    def test_non_dict_response_returns_empty(self) -> None:
        self.assertEqual(decompose_one("decision text", lambda p: "not a dict"), [])

    def test_filters_non_string_entries(self) -> None:
        def caller(prompt: str) -> dict[str, Any]:
            return {"atomic_facts": ["good", 42, "", "  ", "another good"]}

        self.assertEqual(
            decompose_one("decision text", caller),
            ["good", "another good"],
        )


class DecomposeDecisionsWithFlagOnTests(unittest.TestCase):

    def setUp(self) -> None:
        os.environ[ATOMIC_DECOMPOSITION_ENABLED_ENV] = "true"

    def tearDown(self) -> None:
        os.environ.pop(ATOMIC_DECOMPOSITION_ENABLED_ENV, None)

    def test_atomic_facts_attached_when_present(self) -> None:
        def caller(prompt: str) -> dict[str, Any]:
            return {"atomic_facts": ["fact 1"]}

        decisions = [
            {"decision_text": "FCC approved band plan.", "confidence": 0.9},
        ]
        annotated, findings, count = decompose_decisions(
            decisions, api_caller=caller, source_id="s",
        )
        self.assertEqual(count, 1)
        self.assertEqual(annotated[0]["atomic_facts"], ["fact 1"])
        self.assertEqual(annotated[0]["atomic_decomposition_model"], "claude-haiku-4-5-20251001")
        self.assertEqual(findings, [])

    def test_empty_response_emits_warn_finding(self) -> None:
        def caller(prompt: str) -> dict[str, Any]:
            return {"atomic_facts": []}

        decisions = [{"decision_text": "x", "confidence": 0.9}]
        annotated, findings, count = decompose_decisions(
            decisions, api_caller=caller, source_id="s",
        )
        self.assertEqual(count, 1)
        self.assertIsNone(annotated[0]["atomic_facts"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_code, "atomic_decomposition_failed")
        self.assertEqual(findings[0].severity, "warn")

    def test_failing_caller_keeps_processing_other_decisions(self) -> None:
        """RT pass 2: a failed call must NOT block other decisions in the same run."""
        call_index = {"i": 0}

        def caller(prompt: str) -> dict[str, Any]:
            call_index["i"] += 1
            if call_index["i"] == 1:
                raise RuntimeError("first call fails")
            return {"atomic_facts": ["good fact"]}

        decisions = [
            {"decision_text": "first decision", "confidence": 0.9},
            {"decision_text": "second decision", "confidence": 0.9},
        ]
        annotated, findings, count = decompose_decisions(
            decisions, api_caller=caller, source_id="s",
        )
        self.assertEqual(count, 2)
        self.assertIsNone(annotated[0]["atomic_facts"])
        self.assertEqual(annotated[1]["atomic_facts"], ["good fact"])
        self.assertEqual(len(findings), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
