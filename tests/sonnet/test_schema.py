"""Phase 5 — meeting_minutes schema's `prompt_variant` enum.

Three minimal tests covering the schema's contract:

* an artifact carrying each of the four variants validates;
* an artifact without the field validates (backward compat — pre-Phase-5);
* an artifact with an unknown variant fails.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _base_payload() -> dict:
    """Flat-projected meeting_minutes payload with `extraction_config` block.

    The schema is a FLAT projection: it validates
    ``{"artifact_type": "meeting_minutes", **payload}``. Tests must mirror
    that shape — passing the full envelope (with artifact_id /
    content_hash / etc.) trips the top-level
    ``additionalProperties: false``.
    """
    return {
        "artifact_type": "meeting_minutes",
        "title": "T",
        "summary": "S",
        "schema_version": "1.4.0",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "provenance": {
            "produced_by": "meeting_minutes_llm",
            "extraction_config": {
                "temperature": 0.0,
                "seed_inputs": {
                    "model_id": "claude-haiku-4-7",
                    "prompt_content_hash": "p",
                    "transcript_hash": "t",
                },
                "chunks_full_hash": "c",
                "chunk_count": 1,
                "first_chunk_hash": "f",
                "last_chunk_hash": "l",
                "prompt_content_hash": "p",
            },
        },
    }


@pytest.mark.parametrize(
    "variant",
    [
        "production_haiku",
        "haiku_prompt_with_sonnet_model",
        "opus_prompt_with_sonnet_model",
        "opus_baseline",
    ],
)
def test_each_variant_validates(variant: str) -> None:
    schema = _schema()
    art = _base_payload()
    art["provenance"]["extraction_config"]["prompt_variant"] = variant
    jsonschema.Draft202012Validator(schema).validate(art)


def test_artifact_without_prompt_variant_validates() -> None:
    """Backward compat — pre-Phase-5 artifacts omit the field entirely."""
    schema = _schema()
    art = _base_payload()
    # The base payload omits prompt_variant; validation must pass.
    jsonschema.Draft202012Validator(schema).validate(art)


def test_unknown_variant_fails() -> None:
    schema = _schema()
    art = _base_payload()
    art["provenance"]["extraction_config"]["prompt_variant"] = (
        "claude-3.5-sonnet-with-cascade"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(art)


def test_status_report_haiku_f1_out_of_range_fails() -> None:
    """`haiku_latest_f1: 1.5` violates the 0..1 bound."""
    schema_path = _SCHEMA_PATH.parent / "status_report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    rep = {
        "artifact_type": "status_report",
        "schema_version": "1.0.0",
        "manifest_hash": None,
        "generated_at": "2026-05-20T00:00:00+00:00",
        "rows": [
            {
                "source_id": "x",
                "state": "validated",
                "recommendation": "none",
                "haiku_latest_f1": 1.5,
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(rep)


def test_status_report_without_phase5_fields_validates() -> None:
    """Default --show-all-models OFF — rows omit the three Phase-5 fields."""
    schema_path = _SCHEMA_PATH.parent / "status_report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    rep = {
        "artifact_type": "status_report",
        "schema_version": "1.0.0",
        "manifest_hash": None,
        "generated_at": "2026-05-20T00:00:00+00:00",
        "rows": [
            {"source_id": "x", "state": "validated", "recommendation": "none"}
        ],
    }
    jsonschema.Draft202012Validator(schema).validate(rep)


def test_status_report_with_phase5_fields_validates() -> None:
    schema_path = _SCHEMA_PATH.parent / "status_report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    rep = {
        "artifact_type": "status_report",
        "schema_version": "1.0.0",
        "manifest_hash": None,
        "generated_at": "2026-05-20T00:00:00+00:00",
        "rows": [
            {
                "source_id": "x",
                "state": "validated",
                "recommendation": "none",
                "haiku_latest_f1": 0.395,
                "sonnet_latest_f1": None,
                "opus_item_count": 106,
            }
        ],
    }
    jsonschema.Draft202012Validator(schema).validate(rep)
