"""Tests for the Phase 4.A additions to ``comparison_result.schema.json``.

The new fields are ADDITIVE (every one is optional at the schema
layer) so pre-4.A comparison artifacts validate unchanged. PR #233's
byte-equal invariant between ``create_opus_reference_baselines`` and
``compare_opus_haiku`` is unaffected — those scripts have not been
modified by this PR; only the schema has been extended.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "comparison_result.schema.json"
)


PHASE_4A_FIELDS: dict[str, str] = {
    "pre_gate_haiku_count": "integer",
    "pre_gate_haiku_f1": "number",
    "pre_gate_haiku_precision": "number",
    "pre_gate_haiku_recall": "number",
    "post_gate_haiku_count": "integer",
    "post_gate_haiku_f1": "number",
    "post_gate_haiku_precision": "number",
    "post_gate_haiku_recall": "number",
    "grounded_count": "integer",
    "ungrounded_count": "integer",
    "gate_drop_rate": "number",
    "legacy_exempt_count": "integer",
    "recall_collapse_warning": "boolean",
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _minimal_artifact() -> dict:
    """A minimal valid pre-4.A comparison_result artifact (two-way shape)."""
    summary = {
        "total_opus_items": 0,
        "total_haiku_items": 0,
        "true_positives": 0,
        "false_negatives": 0,
        "haiku_only": 0,
        "gt_covered_by_haiku": 0,
        "gt_missed_by_haiku": 0,
        "gt_covered_by_opus": 0,
        "haiku_recall_vs_opus": 0.0,
        "haiku_precision_vs_opus": 0.0,
        "haiku_f1_vs_opus": 0.0,
        "gt_recall_haiku": 0.0,
        "gt_recall_opus": 0.0,
    }
    return {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": "fixture-source",
        "haiku_run_id": "run-001",
        "opus_model_id": "claude-opus-4-x",
        "compared_at": "2026-05-23T12:00:00+00:00",
        "by_type": {},
        "summary": summary,
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
    }


def test_all_phase_4a_fields_present_in_schema(schema: dict) -> None:
    props = schema["properties"]
    for name in PHASE_4A_FIELDS:
        assert name in props, f"{name} missing from comparison_result schema"


def test_phase_4a_fields_have_expected_types(schema: dict) -> None:
    props = schema["properties"]
    for name, expected_type in PHASE_4A_FIELDS.items():
        assert props[name]["type"] == expected_type, (
            f"{name} type drift: {props[name].get('type')!r} != {expected_type!r}"
        )


def test_no_phase_4a_field_is_in_required(schema: dict) -> None:
    """Every new field MUST be optional — pre-4.A artifacts must validate."""
    required = set(schema.get("required", []))
    for name in PHASE_4A_FIELDS:
        assert name not in required, (
            f"{name} became required — that breaks pre-4.A backward compat"
        )


def test_comparison_artifact_with_phase_4a_fields_validates(schema: dict) -> None:
    artifact = _minimal_artifact()
    artifact.update(
        {
            "pre_gate_haiku_count": 50,
            "pre_gate_haiku_f1": 0.55,
            "pre_gate_haiku_precision": 0.50,
            "pre_gate_haiku_recall": 0.60,
            "post_gate_haiku_count": 38,
            "post_gate_haiku_f1": 0.68,
            "post_gate_haiku_precision": 0.75,
            "post_gate_haiku_recall": 0.62,
            "grounded_count": 38,
            "ungrounded_count": 12,
            "gate_drop_rate": 0.24,
            "legacy_exempt_count": 0,
            "recall_collapse_warning": False,
        }
    )
    jsonschema.validate(artifact, schema)


def test_pre_4a_comparison_artifact_still_validates(schema: dict) -> None:
    """Backward-compat: a pre-4.A artifact with NO gate fields validates."""
    jsonschema.validate(_minimal_artifact(), schema)


def test_recall_collapse_warning_must_be_boolean(schema: dict) -> None:
    artifact = _minimal_artifact()
    artifact["recall_collapse_warning"] = "yes"  # wrong type
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(artifact, schema)


def test_gate_drop_rate_bounded_0_to_1(schema: dict) -> None:
    artifact = _minimal_artifact()
    artifact["gate_drop_rate"] = 1.5  # out of bounds
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(artifact, schema)


def test_negative_counts_rejected(schema: dict) -> None:
    artifact = _minimal_artifact()
    artifact["pre_gate_haiku_count"] = -1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(artifact, schema)


def test_f1_outside_unit_interval_rejected(schema: dict) -> None:
    artifact = _minimal_artifact()
    artifact["pre_gate_haiku_f1"] = 1.1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(artifact, schema)
