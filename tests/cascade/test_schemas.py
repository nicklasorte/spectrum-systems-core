"""Phase 6 cascade schema tests.

Asserts:
  * `meeting_minutes_filtered.schema.json` accepts a real filtered
    envelope; rejects unknown fields (`additionalProperties: false`).
  * `cascade_filter_log.schema.json` accepts a real log envelope.
  * `cascade_filter_response.schema.json` rejects {decision: 'maybe'}
    and accepts a valid array.
  * `meeting_minutes_filtered.filtered_items` carries the same 23 array
    keys as `cascade.executor.extraction_array_keys()` — the
    enum-drift catcher.
  * The `prompt_variant` enum on `meeting_minutes.schema.json` and on
    `comparison_result.schema.json` contains
    `production_haiku_with_cascade_filter` and is additive (the four
    pre-Phase-6 values are still valid).
  * `cascade_confirmation_item_threshold` is bounded [10, 500] in
    `cost_constants.schema.json`.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.cascade.executor import extraction_array_keys
from spectrum_systems_core.schemas import schema_path


_CASCADE_RESPONSE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "cascade"
    / "cascade_filter_response.schema.json"
)


def _load_schema(name: str) -> dict:
    if name == "cascade_filter_response":
        return json.loads(
            _CASCADE_RESPONSE_SCHEMA_PATH.read_text(encoding="utf-8")
        )
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# meeting_minutes_filtered.
# ---------------------------------------------------------------------------


def _valid_filtered_envelope() -> dict:
    return {
        "artifact_type": "meeting_minutes_filtered",
        "schema_version": "1.0.0",
        "source_artifact_path": "/lake/store/processed/meetings/x/meeting_minutes__abc.json",
        "filter_metadata": {
            "filter_model": "claude-sonnet-4-6",
            "filter_prompt_path": "workflows/prompts/cascade_filter_sonnet.md",
            "filter_prompt_content_hash": "deadbeef" * 8,
            "items_kept_count": 0,
            "items_dropped_count": 0,
            "chunks_evaluated": 0,
            "chunks_with_invalid_filter_response": 0,
            "truncation_count": 0,
            "filter_started_at": "2026-05-20T12:00:00+00:00",
            "filter_completed_at": "2026-05-20T12:00:01+00:00",
        },
        "filtered_items": {k: [] for k in extraction_array_keys()},
    }


def test_filtered_envelope_validates() -> None:
    schema = _load_schema("meeting_minutes_filtered")
    jsonschema.Draft202012Validator(schema).validate(
        _valid_filtered_envelope()
    )


def test_filtered_envelope_unknown_field_rejected() -> None:
    schema = _load_schema("meeting_minutes_filtered")
    env = _valid_filtered_envelope()
    env["unknown_field"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(env)


def test_filtered_metadata_unknown_field_rejected() -> None:
    schema = _load_schema("meeting_minutes_filtered")
    env = _valid_filtered_envelope()
    env["filter_metadata"]["unknown"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(env)


def test_filtered_items_keys_match_executor_constants() -> None:
    """The schema's filtered_items.properties keys must EXACTLY equal
    cascade.executor.extraction_array_keys()."""
    schema = _load_schema("meeting_minutes_filtered")
    schema_keys = set(
        schema["properties"]["filtered_items"]["properties"].keys()
    )
    executor_keys = set(extraction_array_keys())
    assert schema_keys == executor_keys, (
        f"schema keys diverge from executor constants: "
        f"only_in_schema={schema_keys - executor_keys}, "
        f"only_in_executor={executor_keys - schema_keys}"
    )


# ---------------------------------------------------------------------------
# cascade_filter_response.
# ---------------------------------------------------------------------------


def test_filter_response_accepts_keep_and_drop() -> None:
    schema = _load_schema("cascade_filter_response")
    jsonschema.Draft202012Validator(schema).validate(
        [
            {"item_idx": 0, "decision": "keep", "reason": "ok"},
            {"item_idx": 1, "decision": "drop", "reason": "ok"},
        ]
    )


def test_filter_response_rejects_maybe() -> None:
    schema = _load_schema("cascade_filter_response")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(
            [{"item_idx": 0, "decision": "maybe", "reason": "x"}]
        )


def test_filter_response_rejects_unknown_field() -> None:
    schema = _load_schema("cascade_filter_response")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(
            [
                {
                    "item_idx": 0,
                    "decision": "keep",
                    "reason": "ok",
                    "secret": "no",
                }
            ]
        )


# ---------------------------------------------------------------------------
# cascade_filter_log.
# ---------------------------------------------------------------------------


def _valid_log_envelope() -> dict:
    return {
        "artifact_type": "cascade_filter_log",
        "schema_version": "1.0.0",
        "source_artifact_path": "/lake/source.json",
        "filtered_artifact_path": "/lake/filtered.json",
        "summary": {
            "items_in": 1,
            "items_kept": 1,
            "items_dropped": 0,
            "chunks_evaluated": 1,
            "chunks_with_invalid_filter_response": 0,
            "truncation_count": 0,
            "total_filter_tokens": 100,
            "total_filter_cost_usd": "0.001000",
            "started_at": "2026-05-20T12:00:00+00:00",
            "completed_at": "2026-05-20T12:00:01+00:00",
        },
        "decisions": [
            {
                "chunk_index": 0,
                "item_idx": 0,
                "extraction_type": "decisions",
                "decision": "keep",
                "reason": "ok",
            }
        ],
    }


def test_log_envelope_validates() -> None:
    schema = _load_schema("cascade_filter_log")
    jsonschema.Draft202012Validator(schema).validate(_valid_log_envelope())


def test_log_envelope_unknown_field_rejected() -> None:
    schema = _load_schema("cascade_filter_log")
    env = _valid_log_envelope()
    env["unknown"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(env)


def test_log_invalid_passthrough_decision_accepted() -> None:
    """invalid_response_passthrough is a real decision value the
    executor records on chunks whose filter response failed
    validation; the schema must allow it."""
    schema = _load_schema("cascade_filter_log")
    env = _valid_log_envelope()
    env["decisions"][0]["decision"] = "invalid_response_passthrough"
    jsonschema.Draft202012Validator(schema).validate(env)


# ---------------------------------------------------------------------------
# prompt_variant enum extension.
# ---------------------------------------------------------------------------


def test_meeting_minutes_prompt_variant_enum_extended() -> None:
    schema = _load_schema("meeting_minutes")
    enum = (
        schema["properties"]["provenance"]["properties"]["extraction_config"][
            "properties"
        ]["prompt_variant"]["enum"]
    )
    # Additive: every Phase-5 value still present.
    for value in (
        "production_haiku",
        "haiku_prompt_with_sonnet_model",
        "opus_prompt_with_sonnet_model",
        "opus_baseline",
    ):
        assert value in enum
    assert "production_haiku_with_cascade_filter" in enum


def test_comparison_result_prompt_variant_enum_extended() -> None:
    schema = _load_schema("comparison_result")
    for key in ("haiku_prompt_variant", "sonnet_prompt_variant"):
        enum = schema["properties"][key]["enum"]
        assert "production_haiku_with_cascade_filter" in enum
        for value in (
            "production_haiku",
            "haiku_prompt_with_sonnet_model",
            "opus_prompt_with_sonnet_model",
            "opus_baseline",
        ):
            assert value in enum


# ---------------------------------------------------------------------------
# cost_constants — threshold bounds.
# ---------------------------------------------------------------------------


def test_cost_constants_threshold_in_schema() -> None:
    schema = _load_schema("cost_constants")
    prop = schema["properties"]["cascade_confirmation_item_threshold"]
    assert prop["type"] == "integer"
    assert prop["minimum"] == 10
    assert prop["maximum"] == 500


def test_shipped_constants_threshold_is_within_bounds(tmp_path: Path) -> None:
    """The shipped data/cost_constants.json validates with the threshold
    addition AND the threshold is loadable via the helper."""
    from spectrum_systems_core.cost.estimator import (
        load_cascade_confirmation_item_threshold,
        load_cost_constants,
    )

    doc = load_cost_constants()
    assert "cascade_confirmation_item_threshold" in doc
    t = load_cascade_confirmation_item_threshold()
    assert 10 <= t <= 500


def test_cost_constants_threshold_out_of_range_rejected(tmp_path: Path) -> None:
    from spectrum_systems_core.cost.estimator import (
        CostConstantsError,
        load_cost_constants,
    )

    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "cost_constants",
                "schema_version": "1.0.0",
                "currency": "USD",
                "constants": {
                    "claude-haiku-4-7": {
                        "input_per_million_tokens": 0.25,
                        "output_per_million_tokens": 1.25,
                    }
                },
                "cascade_confirmation_item_threshold": 1,
                "last_updated": "2026-05-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CostConstantsError):
        load_cost_constants(p)

    p.write_text(
        json.dumps(
            {
                "artifact_type": "cost_constants",
                "schema_version": "1.0.0",
                "currency": "USD",
                "constants": {
                    "claude-haiku-4-7": {
                        "input_per_million_tokens": 0.25,
                        "output_per_million_tokens": 1.25,
                    }
                },
                "cascade_confirmation_item_threshold": 600,
                "last_updated": "2026-05-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CostConstantsError):
        load_cost_constants(p)
