"""Phase X-2 unit tests: schema validation gate + SCHEMA_VALIDATION_ENABLED bypass.

Covers ``spectrum_systems_core.validation.validate_artifact`` and the
per-artifact-type schemas under
``spectrum_systems_core/schemas/``. Tests both happy-path acceptance
and the rejection modes the X-2 spec calls out: missing required
field, ``artifact_kind`` smuggled into a payload, unknown artifact_type
raising ``SchemaNotFoundError``, and the env-var bypass writing a
warning to the logger.
"""
from __future__ import annotations

import logging
import os
import unittest
from unittest import mock

from spectrum_systems_core.schemas import schema_path
from spectrum_systems_core.validation import (
    ArtifactValidationError,
    SCHEMA_VALIDATION_ENV_VAR,
    SchemaNotFoundError,
    _load_schema,
    validate_artifact,
)


def _orchestration_artifact(**overrides):
    base = {
        "artifact_type": "orchestration_result",
        "schema_version": "1.0.0",
        "run_id": "tex-abc",
        "source_id": "src-1",
        "chunks_attempted": 10,
        "chunks_succeeded": 8,
        "chunks_blocked": 2,
        "block_reasons": {
            "rate_limit_exhausted": 1,
            "empty_response": 1,
            "parse_error": 0,
            "other": 0,
        },
        "stage_status": "partial",
    }
    base.update(overrides)
    return base


class ValidateArtifactHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        _load_schema.cache_clear()

    def test_orchestration_result_valid_artifact_passes(self) -> None:
        validate_artifact(_orchestration_artifact(), "orchestration_result")

    def test_orchestration_result_accepts_spurious_add_count(self) -> None:
        # Phase Z.4: the new optional field validates at 0 and > 0.
        validate_artifact(
            _orchestration_artifact(spurious_add_count=0),
            "orchestration_result",
        )
        validate_artifact(
            _orchestration_artifact(spurious_add_count=3),
            "orchestration_result",
        )

    def test_orchestration_result_additive_without_spurious_add_count(
        self,
    ) -> None:
        # Backward-compat / additive proof: an artifact written before
        # Phase Z.4 (no spurious_add_count key at all) is still valid
        # because the property is optional, not in 'required'.
        art = _orchestration_artifact()
        self.assertNotIn("spurious_add_count", art)
        validate_artifact(art, "orchestration_result")

    def test_calibration_warning_valid_artifact_passes(self) -> None:
        validate_artifact(
            {
                "artifact_type": "calibration_warning",
                "schema_version": "1.0.0",
                "run_id": "tex-abc",
                "high_confidence_rate": 0.9,
                "threshold": 0.8,
                "finding": "More than 80% of items have confidence > 0.85.",
            },
            "calibration_warning",
        )

    def test_api_rate_limit_exhausted_valid_passes(self) -> None:
        validate_artifact(
            {
                "artifact_type": "api_rate_limit_exhausted",
                "schema_version": "1.0.0",
                "failure_id": "f-1",
                "chunk_id": "c-1",
                "source_id": "src",
                "component": "caller",
                "detail": "exhausted",
                "created_at": "2026-05-12T00:00:00+00:00",
            },
            "api_rate_limit_exhausted",
        )


class ValidateArtifactRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        _load_schema.cache_clear()

    def test_missing_required_field_raises(self) -> None:
        bad = _orchestration_artifact()
        del bad["chunks_attempted"]
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "orchestration_result")

    def test_artifact_kind_in_payload_fails(self) -> None:
        # The X-2 spec is explicit: "Never use artifact_kind". Any
        # schema with additionalProperties: false will reject it.
        bad = _orchestration_artifact()
        bad["artifact_kind"] = "orchestration_result"
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "orchestration_result")

    def test_negative_spurious_add_count_fails(self) -> None:
        bad = _orchestration_artifact(spurious_add_count=-1)
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "orchestration_result")

    def test_non_integer_spurious_add_count_fails(self) -> None:
        bad = _orchestration_artifact(spurious_add_count="3")
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "orchestration_result")

    def test_unknown_artifact_type_raises_schema_not_found(self) -> None:
        with self.assertRaises(SchemaNotFoundError):
            validate_artifact(
                {"artifact_type": "no_such_thing", "schema_version": "1.0.0"},
                "no_such_thing",
            )

    def test_confidence_out_of_range_fails_on_typed_extraction(self) -> None:
        bad = {
            "artifact_type": "typed_extraction",
            "schema_version": "1.0.0",
            "source_id": "src",
            "extraction_run_id": "tex-1",
            "decisions": [
                {
                    "decision_text": "do it",
                    "decision_type": "approved",
                    "source_turn_ids": ["t1"],
                    "confidence": 1.5,
                }
            ],
            "claims": [],
            "action_items": [],
        }
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "typed_extraction")

    def test_decision_missing_confidence_fails(self) -> None:
        bad = {
            "artifact_type": "typed_extraction",
            "schema_version": "1.0.0",
            "source_id": "src",
            "extraction_run_id": "tex-1",
            "decisions": [
                {
                    "decision_text": "do it",
                    "decision_type": "approved",
                    "source_turn_ids": ["t1"],
                }
            ],
            "claims": [],
            "action_items": [],
        }
        with self.assertRaises(ArtifactValidationError):
            validate_artifact(bad, "typed_extraction")

    def test_action_item_does_not_require_confidence(self) -> None:
        # X-3 spec: confidence is required on decisions + claims, but
        # NOT on action_items (deterministic owner + action).
        good = {
            "artifact_type": "typed_extraction",
            "schema_version": "1.0.0",
            "source_id": "src",
            "extraction_run_id": "tex-1",
            "decisions": [],
            "claims": [],
            "action_items": [
                {
                    "action": "ship it",
                    "owner": "alice",
                    "source_turn_ids": ["t1"],
                }
            ],
        }
        validate_artifact(good, "typed_extraction")


class SchemaMetaValidationTests(unittest.TestCase):
    """Every schema file must itself be a valid JSON Schema draft 2020-12."""

    def test_all_schemas_meta_validate(self) -> None:
        import json

        import jsonschema

        from spectrum_systems_core.schemas import SCHEMAS_DIR

        files = sorted(SCHEMAS_DIR.glob("*.schema.json"))
        # Guard: the directory must contain at least the schemas listed
        # in the X-2 spec. Catches a removed schema during refactor.
        names = {p.stem.replace(".schema", "") for p in files}
        required = {
            "orchestration_result",
            "calibration_warning",
            "typed_extraction",
            "meeting_extraction",
            "api_rate_limit_exhausted",
            "extraction_empty_response",
            "typed_extraction_llm_json_parse_failed",
            "typed_extraction_empty_result",
        }
        missing = required - names
        self.assertFalse(missing, f"missing schema files: {missing}")

        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(doc)


class SchemaValidationBypassTests(unittest.TestCase):
    def setUp(self) -> None:
        _load_schema.cache_clear()

    def test_env_var_false_bypasses_validation_and_logs_warning(self) -> None:
        # The bypass is an X-2 rollback path -- it must be auditable
        # via a logged warning.
        bad = _orchestration_artifact()
        del bad["chunks_attempted"]
        with mock.patch.dict(
            os.environ, {SCHEMA_VALIDATION_ENV_VAR: "false"}
        ), self.assertLogs(
            "spectrum_systems_core.validation", level="WARNING"
        ) as captured:
            # Validation MUST NOT raise.
            validate_artifact(bad, "orchestration_result")
        # Warning must surface the env var so an operator can grep for it.
        joined = "\n".join(captured.output)
        self.assertIn(SCHEMA_VALIDATION_ENV_VAR, joined)
        self.assertIn("schema_validation_disabled", joined)

    def test_env_var_unset_enforces_validation(self) -> None:
        bad = _orchestration_artifact()
        del bad["chunks_attempted"]
        # Remove the env var explicitly to guard against leftover state.
        env = {k: v for k, v in os.environ.items() if k != SCHEMA_VALIDATION_ENV_VAR}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ArtifactValidationError):
                validate_artifact(bad, "orchestration_result")


if __name__ == "__main__":
    unittest.main()
