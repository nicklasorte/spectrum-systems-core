"""Schema tests for agenda_item.schema.json (Phase W.0)."""
from __future__ import annotations

import json
import pathlib
import uuid

import jsonschema
import pytest

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "agenda"
    / "agenda_item.schema.json"
)


def _schema():
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _valid_payload(**overrides):
    payload = {
        "agenda_item_id": str(uuid.uuid4()),
        "artifact_type": "agenda_item",
        "schema_version": "1.0.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "trace_id": str(uuid.uuid4()),
        "pipeline_run_id": str(uuid.uuid4()),
        "source_id": "smoke-test-fixture",
        "ordinal": 1,
        "label": "FSS Protection Discussion",
        "start_turn_id": "fixture-001",
        "end_turn_id": "fixture-005",
        "detection_method": "llm_detected",
        "detection_confidence": 0.85,
        "detector_model_used": "claude-sonnet-4-6",
        "provenance": {
            "produced_by": "AgendaDetector",
            "detected_from": "first_20_percent_of_turns",
        },
    }
    payload.update(overrides)
    return payload


def test_agenda_item_schema_validates_required_fields():
    schema = _schema()
    jsonschema.Draft202012Validator(schema).validate(_valid_payload())


def test_agenda_item_schema_rejects_missing_label():
    schema = _schema()
    payload = _valid_payload()
    del payload["label"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_rejects_invalid_ordinal_zero():
    schema = _schema()
    payload = _valid_payload(ordinal=0)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_rejects_invalid_detection_method_enum():
    schema = _schema()
    payload = _valid_payload(detection_method="guessed")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_rejects_label_too_long():
    schema = _schema()
    payload = _valid_payload(label="x" * 201)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_rejects_unknown_top_level_key():
    schema = _schema()
    payload = _valid_payload(extra_field="not allowed")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_allows_null_trace_id_and_confidence():
    schema = _schema()
    payload = _valid_payload(trace_id=None, detection_confidence=None)
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_agenda_item_schema_uses_artifact_type_not_kind():
    """RT3: enforce constitutional naming -- artifact_type, never artifact_kind."""
    schema = _schema()
    schema_text = _SCHEMA_PATH.read_text(encoding="utf-8")
    assert "artifact_kind" not in schema_text
    assert schema["properties"]["artifact_type"]["const"] == "agenda_item"
