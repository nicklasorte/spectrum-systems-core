"""Tests for the chunk.schema.json agenda_item_id field (Phase W.2).

The field is OPTIONAL -- old chunks without it must still validate so a
flag-off rollback does not invalidate any persisted chunks.
"""
from __future__ import annotations

import json
import pathlib
import uuid

import jsonschema
import pytest

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "contracts" / "schemas" / "chunk.schema.json"
)


def _schema():
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _valid_chunk(**overrides):
    chunk = {
        "chunk_id": str(uuid.uuid4()),
        "source_id": "smoke-test-fixture",
        "source_family": "meetings",
        "chunk_index": 0,
        "unit_ids": [str(uuid.uuid4())],
        "text": "some text",
        "text_hash": "sha256:" + "0" * 64,
        "unit_count": 1,
        "overlap_unit_id": None,
        "page_numbers": [],
        "char_count": 9,
    }
    chunk.update(overrides)
    return chunk


def test_chunk_schema_validates_without_agenda_item_id():
    """Pre-Phase W chunks (no field) must still validate."""
    jsonschema.Draft202012Validator(_schema()).validate(_valid_chunk())


def test_chunk_schema_validates_with_agenda_item_id_string():
    validator = jsonschema.Draft202012Validator(_schema())
    validator.validate(_valid_chunk(agenda_item_id=str(uuid.uuid4())))


def test_chunk_schema_validates_with_agenda_item_id_null():
    """Null is the explicit "Phase W enabled but no agenda for this
    chunk" state. Must validate.
    """
    validator = jsonschema.Draft202012Validator(_schema())
    validator.validate(_valid_chunk(agenda_item_id=None))


def test_chunk_schema_rejects_agenda_item_id_non_string():
    validator = jsonschema.Draft202012Validator(_schema())
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(_valid_chunk(agenda_item_id=42))
