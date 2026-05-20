"""Phase 2R — schema validation tests for the report and config schemas."""
from __future__ import annotations

import json

import jsonschema
import pytest

from spectrum_systems_core.schemas import schema_path
from spectrum_systems_core.transcript_quality import (
    report_to_dict,
    validate,
)
from spectrum_systems_core.transcript_quality._config_loader import (
    DEFAULT_CONFIG_PATH,
    TranscriptQualityConfigError,
    load_config,
    validate_config,
)

from . import fixtures as F


def _load_schema(name: str) -> dict:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def test_report_schema_accepts_validator_output_for_valid_transcript() -> None:
    report = validate(F.valid_transcript())
    payload = report_to_dict(report)
    schema = _load_schema("transcript_quality_report")
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_report_schema_accepts_failure_report() -> None:
    report = validate(F.encoding_corrupted_transcript())
    payload = report_to_dict(report)
    schema = _load_schema("transcript_quality_report")
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_report_schema_rejects_unknown_top_level_key() -> None:
    report = validate(F.valid_transcript())
    payload = report_to_dict(report)
    payload["unknown_top_level"] = True
    schema = _load_schema("transcript_quality_report")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_report_schema_rejects_unknown_check_key() -> None:
    report = validate(F.valid_transcript())
    payload = report_to_dict(report)
    payload["checks"][0]["unknown"] = "x"
    schema = _load_schema("transcript_quality_report")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_default_config_loads_and_validates() -> None:
    config = load_config()
    assert config["schema_version"] == "1.0.0"
    assert config["hard_max_byte_length"] == 10_000_000


def test_default_config_path_is_repo_root_data_file() -> None:
    assert DEFAULT_CONFIG_PATH.name == "transcript_quality_config.json"


def test_config_schema_rejects_hard_max_above_10m() -> None:
    bad = {
        "schema_version": "1.0.0",
        "min_byte_length": 500,
        "advisory_max_byte_length": 1_000_000,
        "hard_max_byte_length": 20_000_000,
        "min_turn_count": 2,
        "min_word_count_when_single_speaker": 100,
    }
    with pytest.raises(TranscriptQualityConfigError):
        validate_config(bad)


def test_config_schema_rejects_advisory_above_hard_max() -> None:
    bad = {
        "schema_version": "1.0.0",
        "min_byte_length": 500,
        "advisory_max_byte_length": 9_000_000,
        "hard_max_byte_length": 1_000_000,
        "min_turn_count": 2,
        "min_word_count_when_single_speaker": 100,
    }
    with pytest.raises(TranscriptQualityConfigError):
        validate_config(bad)


def test_config_schema_rejects_unknown_field() -> None:
    bad = {
        "schema_version": "1.0.0",
        "min_byte_length": 500,
        "advisory_max_byte_length": 1_000_000,
        "hard_max_byte_length": 10_000_000,
        "min_turn_count": 2,
        "min_word_count_when_single_speaker": 100,
        "unknown_field": "x",
    }
    with pytest.raises(TranscriptQualityConfigError):
        validate_config(bad)
