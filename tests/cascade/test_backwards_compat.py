"""Phase 6 backwards-compatibility tests.

Pass 3 #6 says: default behaviour without `--enable-cascade-filter`
must be byte-identical to pre-Phase-6. We assert:

  * A meeting_minutes envelope with `prompt_variant = "production_haiku"`
    still validates under the extended enum (additive change).
  * A comparison_result envelope at the two-way schema branch still
    validates with the extended enum.
  * A meeting_minutes envelope WITHOUT a `prompt_variant` key validates
    (the field is optional — pre-Phase-5 artifacts).
"""
from __future__ import annotations

import json

import jsonschema

from spectrum_systems_core.schemas import schema_path


def _schema(name: str) -> dict:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def _minimal_mm(prompt_variant=None) -> dict:
    payload = {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "title": "x",
        "summary": "",
        "decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    if prompt_variant is not None:
        payload["provenance"] = {
            "produced_by": "meeting_minutes_llm",
            "extraction_config": {
                "temperature": 0.0,
                "seed_inputs": {
                    "model_id": "claude-haiku-4-7",
                    "prompt_content_hash": "a" * 8,
                    "transcript_hash": "b" * 8,
                },
                "chunks_full_hash": "c" * 8,
                "chunk_count": 1,
                "first_chunk_hash": "d" * 8,
                "last_chunk_hash": "e" * 8,
                "prompt_content_hash": "f" * 8,
                "prompt_variant": prompt_variant,
            },
        }
    return payload


def test_pre_phase_6_artifact_validates_against_extended_enum() -> None:
    schema = _schema("meeting_minutes")
    jsonschema.Draft202012Validator(schema).validate(
        _minimal_mm(prompt_variant="production_haiku")
    )


def test_no_prompt_variant_field_validates() -> None:
    """Pre-Phase-5 artifacts omitted prompt_variant entirely."""
    schema = _schema("meeting_minutes")
    jsonschema.Draft202012Validator(schema).validate(_minimal_mm())


def test_new_prompt_variant_validates() -> None:
    schema = _schema("meeting_minutes")
    jsonschema.Draft202012Validator(schema).validate(
        _minimal_mm(
            prompt_variant="production_haiku_with_cascade_filter"
        )
    )


def test_unknown_prompt_variant_rejected() -> None:
    schema = _schema("meeting_minutes")
    import pytest

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(
            _minimal_mm(prompt_variant="wholly_new_variant")
        )


def test_comparison_result_legacy_validates() -> None:
    """A pre-Phase-6 two-way comparison_result still validates with
    the extended enum (additive)."""
    schema = _schema("comparison_result")
    legacy = {
        "artifact_type": "comparison_result",
        "schema_version": "1.0.0",
        "source_id": "x",
        "haiku_run_id": "r",
        "opus_model_id": "m",
        "compared_at": "2026-05-20T12:00:00+00:00",
        "summary": {
            "total_opus_items": 10,
            "total_haiku_items": 10,
            "true_positives": 5,
            "false_negatives": 5,
            "haiku_only": 5,
            "gt_covered_by_haiku": 0,
            "gt_missed_by_haiku": 0,
            "gt_covered_by_opus": 0,
            "haiku_recall_vs_opus": 0.5,
            "haiku_precision_vs_opus": 0.5,
            "haiku_f1_vs_opus": 0.5,
            "gt_recall_haiku": 0.0,
            "gt_recall_opus": 0.0,
        },
        "by_type": {},
        "false_negatives": [],
        "haiku_only_items": [],
        "gt_missed": [],
        "gt_pairs_present": False,
        "legacy_eval": True,
    }
    jsonschema.Draft202012Validator(schema).validate(legacy)
