"""Phase R.2: binding validation tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction.binding_validator import (
    REGULATORY_VERBS,
    REQUIRED_DECISION_FIELDS,
    annotate_and_collect_warnings,
    build_binding_warning,
    validate_decision_binding,
    write_binding_warnings,
)
from spectrum_systems_core.validation import validate_artifact


def _good_decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision_text": "FCC approved coordination procedures for 12.7 GHz.",
        "decision_type": "approved",
        "stakeholders": ["FCC"],
        "source_turn_ids": ["c-1"],
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


class ValidateDecisionBindingTests(unittest.TestCase):
    def test_well_formed_decision_with_one_verb(self) -> None:
        result = validate_decision_binding(_good_decision())
        self.assertTrue(result["binding_valid"])
        self.assertTrue(result["regulatory_verb_found"])
        self.assertEqual(result["regulatory_verb_count"], 1)
        self.assertFalse(result["binding_weak"])
        self.assertFalse(result["binding_ambiguous"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["warnings"], [])

    def test_missing_stakeholders_is_invalid(self) -> None:
        d = _good_decision()
        d.pop("stakeholders")
        result = validate_decision_binding(d)
        self.assertFalse(result["binding_valid"])
        self.assertIn("stakeholders", result["missing_fields"])
        self.assertTrue(
            any("missing_required_fields" in w for w in result["warnings"])
        )

    def test_empty_stakeholders_is_invalid(self) -> None:
        d = _good_decision(stakeholders=[])
        result = validate_decision_binding(d)
        self.assertFalse(result["binding_valid"])
        self.assertIn("stakeholders", result["missing_fields"])

    def test_zero_regulatory_verbs_is_weak(self) -> None:
        d = _good_decision(
            decision_text="The committee discussed 12.7 GHz coordination.",
        )
        result = validate_decision_binding(d)
        # Required fields all present → binding_valid stays True; but
        # binding_weak fires because "discussed" is not a regulatory
        # verb.
        self.assertTrue(result["binding_valid"])
        self.assertTrue(result["binding_weak"])
        self.assertEqual(result["regulatory_verb_count"], 0)
        self.assertIn(
            "binding_weak:zero_regulatory_verbs", result["warnings"],
        )

    def test_multiple_regulatory_verbs_is_ambiguous(self) -> None:
        d = _good_decision(
            decision_text="FCC approved 12.7 GHz but rejected the 6 GHz extension.",
        )
        result = validate_decision_binding(d)
        self.assertTrue(result["binding_ambiguous"])
        self.assertEqual(result["regulatory_verb_count"], 2)
        self.assertTrue(
            any("binding_ambiguous" in w for w in result["warnings"])
        )

    def test_empty_source_turns_is_invalid(self) -> None:
        d = _good_decision(source_turn_ids=[])
        result = validate_decision_binding(d)
        self.assertFalse(result["binding_valid"])
        # Either spelling counts as missing when empty.
        self.assertIn("source_turns", result["missing_fields"])

    def test_source_turns_alternate_spelling_accepted(self) -> None:
        d = _good_decision()
        d.pop("source_turn_ids")
        d["source_turns"] = ["c-1"]
        result = validate_decision_binding(d)
        self.assertTrue(result["binding_valid"])

    def test_regulatory_verb_match_is_whole_word(self) -> None:
        # "disapproved" must NOT count as "approved".
        d = _good_decision(
            decision_text="The committee disapproved the proposal.",
        )
        result = validate_decision_binding(d)
        self.assertEqual(result["regulatory_verb_count"], 0)

    def test_regulatory_verbs_constant_matches_task(self) -> None:
        for v in [
            "approved", "rejected", "deferred", "noted", "required",
            "recommended", "prohibited", "authorized", "designated",
        ]:
            self.assertIn(v, REGULATORY_VERBS)


class BindingWarningArtifactTests(unittest.TestCase):
    def test_artifact_passes_schema_validation(self) -> None:
        d = _good_decision(stakeholders=[])  # invalid → warnings
        result = validate_decision_binding(d)
        artifact = build_binding_warning(
            d, result, source_id="src-001", extraction_run_id="tex-abc",
        )
        validate_artifact(artifact, "binding_warning")  # raises if not

    def test_warning_emitted_even_when_binding_invalid_no_halt(self) -> None:
        # Binding validation is a warning, not a halt: annotate_and_collect
        # must still return the decision (annotated with binding_valid=
        # false) alongside the warning artifact. The orchestrator does
        # not drop the decision from the canonical artifact.
        d = _good_decision()
        d.pop("stakeholders")
        annotated, warnings = annotate_and_collect_warnings(
            [d], source_id="src-001",
        )
        self.assertEqual(len(annotated), 1)
        self.assertFalse(annotated[0]["binding_valid"])
        self.assertEqual(len(warnings), 1)

    def test_binding_valid_annotation_on_decision(self) -> None:
        annotated, _ = annotate_and_collect_warnings(
            [_good_decision()], source_id="src-001",
        )
        self.assertIn("binding_valid", annotated[0])
        self.assertTrue(annotated[0]["binding_valid"])

    def test_write_binding_warnings_to_disk(self) -> None:
        d = _good_decision(stakeholders=[])
        annotated, warnings = annotate_and_collect_warnings(
            [d], source_id="src-001",
        )
        with tempfile.TemporaryDirectory() as td:
            paths = write_binding_warnings(warnings, Path(td))
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].is_file())

    def test_required_decision_fields_constant(self) -> None:
        self.assertEqual(
            set(REQUIRED_DECISION_FIELDS),
            {"decision_text", "decision_type", "stakeholders", "source_turns"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
