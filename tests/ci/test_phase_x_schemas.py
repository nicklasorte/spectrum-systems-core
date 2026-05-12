"""CI check: Phase X schemas in ``src/spectrum_systems_core/schemas/``.

X-2 spec: every ``*.schema.json`` file under the package schemas
directory must be a valid JSON Schema draft 2020-12 document, and
must not declare ``artifact_kind`` (the legacy field name banned by
Phase N).

The schemas in this directory are the source of truth for the
write-time validation gate; if any one of them is malformed, the
gate either refuses to load it (raising ``SchemaNotFoundError``-
adjacent failures at import) or accepts whatever shape the operator
typed. Meta-validation here makes a broken schema fail CI before
the gate ever runs.
"""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest


SCHEMAS_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "schemas"
)


def _schema_files() -> list[pathlib.Path]:
    if not SCHEMAS_DIR.is_dir():
        return []
    return sorted(SCHEMAS_DIR.glob("*.schema.json"))


def test_schemas_directory_exists() -> None:
    assert SCHEMAS_DIR.is_dir(), (
        f"Phase X schemas directory missing: {SCHEMAS_DIR}"
    )


@pytest.mark.parametrize("path", _schema_files())
def test_schema_is_valid_draft_2020_12(path: pathlib.Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(doc)


@pytest.mark.parametrize("path", _schema_files())
def test_schema_declares_artifact_type_not_artifact_kind(
    path: pathlib.Path,
) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    properties = doc.get("properties") or {}
    required = doc.get("required") or []
    assert "artifact_kind" not in properties, (
        f"{path.name} declares the legacy 'artifact_kind' field; "
        "use 'artifact_type' (X-2 spec rejects 'artifact_kind')."
    )
    assert "artifact_kind" not in required, (
        f"{path.name} requires the legacy 'artifact_kind' field"
    )
    assert "artifact_type" in properties, (
        f"{path.name} does not declare 'artifact_type'"
    )
    assert "artifact_type" in required, (
        f"{path.name} does not require 'artifact_type'"
    )


@pytest.mark.parametrize("path", _schema_files())
def test_schema_requires_schema_version(path: pathlib.Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    required = doc.get("required") or []
    assert "schema_version" in required, (
        f"{path.name} does not require 'schema_version'"
    )


@pytest.mark.parametrize("path", _schema_files())
def test_schema_forbids_additional_properties(path: pathlib.Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc.get("additionalProperties") is False, (
        f"{path.name} must declare 'additionalProperties: false' so an "
        "artifact with stray fields fails the X-2 gate."
    )


def test_required_phase_x_schemas_present() -> None:
    """The X-2 spec enumerates the schema files the gate depends on."""
    required = {
        "orchestration_result.schema.json",
        "calibration_warning.schema.json",
        "typed_extraction.schema.json",
        "meeting_extraction.schema.json",
        "api_rate_limit_exhausted.schema.json",
        "extraction_empty_response.schema.json",
        "typed_extraction_llm_json_parse_failed.schema.json",
        "typed_extraction_empty_result.schema.json",
    }
    actual = {p.name for p in _schema_files()}
    missing = required - actual
    assert not missing, f"missing X-2 schemas: {missing}"
