"""Phase T.1 tests: regulatory-verb taxonomy + decision_outcome field."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from spectrum_systems_core.config import taxonomy
from spectrum_systems_core.extraction import binding_validator
from spectrum_systems_core.extraction._prompt_blocks import (
    REGULATORY_TAXONOMY_BLOCK,
)
from spectrum_systems_core.extraction.binding_validator import (
    BINDING_VALIDATOR_HALT_ENABLED_ENV,
    binding_validator_halt_enabled,
    build_taxonomy_finding,
    validate_decision_binding,
)
from spectrum_systems_core.extraction.decision_extractor import (
    DecisionExtractor,
)
from spectrum_systems_core.health.finding import (
    ALL_FINDING_CODES,
    HALT_FINDING_CODES,
)


class CanonicalTaxonomySourceTests(unittest.TestCase):
    """RT pass 1 / 2: prove a single Python object backs both consumers."""

    def test_binding_validator_imports_same_object(self) -> None:
        # The taxonomy module is the canonical source; the binding
        # validator's bound name must be the *same* object so a future
        # mutation in one location cannot drift the other.
        self.assertIs(binding_validator.REGULATORY_VERBS, taxonomy.REGULATORY_VERBS)

    def test_prompt_block_contains_taxonomy_verbs(self) -> None:
        # The rendered prompt block must contain at least five of the
        # canonical verbs verbatim so the model is exposed to the
        # taxonomy at inference time.
        seen = sum(1 for v in taxonomy.REGULATORY_VERBS if v in REGULATORY_TAXONOMY_BLOCK)
        self.assertGreaterEqual(seen, 5)

    def test_prompt_block_declares_decision_outcome(self) -> None:
        self.assertIn("decision_outcome", REGULATORY_TAXONOMY_BLOCK)
        for outcome in taxonomy.DECISION_OUTCOME_TYPES:
            self.assertIn(outcome, REGULATORY_TAXONOMY_BLOCK)

    def test_unclassified_sentinel_disjoint_from_real_verbs(self) -> None:
        # The indeterminate-verb sentinel must never collide with a real
        # decision / ambiguous verb, or a future taxonomy edit could
        # silently turn the "no verb claim" marker into a recognised
        # classification (or vice versa) — exactly the drift class this
        # module exists to prevent.
        self.assertNotIn(
            taxonomy.UNCLASSIFIED_DECISION_VERB, taxonomy.DECISION_VERBS
        )
        self.assertNotIn(
            taxonomy.UNCLASSIFIED_DECISION_VERB, taxonomy.AMBIGUOUS_VERBS
        )


class TaxonomyHaltFindingTests(unittest.TestCase):
    """Phase T.1: the finding-builder respects the env flag."""

    def setUp(self) -> None:
        os.environ.pop(BINDING_VALIDATOR_HALT_ENABLED_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(BINDING_VALIDATOR_HALT_ENABLED_ENV, None)

    def _weak_decision(self) -> Dict[str, Any]:
        return {
            "decision_text": "The group discussed the band plan briefly.",
            "decision_type": "noted",
            "stakeholders": ["TIG"],
            "source_turn_ids": ["c-1"],
            "confidence": 0.7,
        }

    def test_finding_severity_warn_by_default(self) -> None:
        result = validate_decision_binding(self._weak_decision())
        finding = build_taxonomy_finding(self._weak_decision(), result)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.severity, "warn")
        # RT pass 1: the finding must include the searched verbs so the
        # operator can diagnose from the artifact alone.
        self.assertIn("searched_verbs", finding.context)
        self.assertIn("decision_text", finding.context)

    def test_finding_severity_halt_when_flag_enabled(self) -> None:
        os.environ[BINDING_VALIDATOR_HALT_ENABLED_ENV] = "true"
        self.assertTrue(binding_validator_halt_enabled())
        result = validate_decision_binding(self._weak_decision())
        finding = build_taxonomy_finding(self._weak_decision(), result)
        self.assertEqual(finding.severity, "halt")

    def test_finding_returns_none_when_verb_present(self) -> None:
        good = {
            "decision_text": "FCC approved the proposal.",
            "decision_type": "approved",
            "stakeholders": ["FCC"],
            "source_turn_ids": ["c-1"],
            "confidence": 0.9,
        }
        result = validate_decision_binding(good)
        self.assertIsNone(build_taxonomy_finding(good, result))


class FindingRegistryTests(unittest.TestCase):
    """Phase T.1 / .2 / .3 / .4 / .5 / .6 / .7: codes registered."""

    def test_taxonomy_code_in_registry(self) -> None:
        self.assertIn("taxonomy_regulatory_verb_missing", ALL_FINDING_CODES)
        self.assertIn("taxonomy_regulatory_verb_missing", HALT_FINDING_CODES)

    def test_all_phase_t_codes_in_registry(self) -> None:
        expected = {
            "spurious_add_rate_elevated",
            "speaker_attribution_missing",
            "chunk_split_mid_turn_detected",
            "ground_truth_missing_type",
            "low_confidence_extraction",
            "correction_candidate_expired",
            "atomic_decomposition_failed",
        }
        self.assertTrue(expected.issubset(ALL_FINDING_CODES))


class DecisionExtractorOutcomeFieldTests(unittest.TestCase):
    """Phase T.1: ``decision_outcome`` flows through when the model emits it."""

    def _api_caller_factory(self, decision_outcome: str | None) -> Any:
        def fake(prompt: str) -> Dict[str, Any]:
            item = {
                "decision_text": "FCC deferred the band plan pending study.",
                "decision_type": "deferred",
                "stakeholders": ["FCC"],
                "source_turn_ids": ["c-1"],
                "confidence": 0.85,
            }
            if decision_outcome is not None:
                item["decision_outcome"] = decision_outcome
            return {"items": [item]}

        return fake

    def test_outcome_preserved_when_valid(self) -> None:
        extractor = DecisionExtractor(api_caller=self._api_caller_factory("deferral"))
        out = extractor.extract([{"chunk_id": "c-1", "text": "...", "speaker": "Alice"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["decision_outcome"], "deferral")

    def test_outcome_dropped_when_invalid(self) -> None:
        extractor = DecisionExtractor(
            api_caller=self._api_caller_factory("not-a-real-outcome"),
        )
        out = extractor.extract([{"chunk_id": "c-1", "text": "...", "speaker": "Alice"}])
        self.assertEqual(len(out), 1)
        self.assertNotIn("decision_outcome", out[0])

    def test_outcome_absent_when_model_omits(self) -> None:
        extractor = DecisionExtractor(api_caller=self._api_caller_factory(None))
        out = extractor.extract([{"chunk_id": "c-1", "text": "...", "speaker": "Alice"}])
        self.assertEqual(len(out), 1)
        self.assertNotIn("decision_outcome", out[0])


class PromptInjectionTests(unittest.TestCase):
    """The decision extractor's rendered prompt must carry the taxonomy block."""

    def test_decision_prompt_contains_taxonomy_block(self) -> None:
        extractor = DecisionExtractor()
        prompt = extractor._build_prompt(
            [{"chunk_id": "c-1", "speaker": "Alice", "text": "Some text."}],
            glossary_block="",
            few_shot_block="",
        )
        self.assertIn(REGULATORY_TAXONOMY_BLOCK, prompt)


class ClaudeMdMentionsTaxonomyTests(unittest.TestCase):
    """RT pass 2: CLAUDE.md must reference taxonomy.py as canonical."""

    def test_claude_md_taxonomy_section(self) -> None:
        path = Path(__file__).resolve().parents[2] / "CLAUDE.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("taxonomy.py", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
