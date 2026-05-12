"""Tests for AgendaDetector (Phase W.1).

Each test exercises the real ``detect()`` path with a stub api_caller so
the validators (Attack 1, Attack 9) actually fire -- not the helpers in
isolation. RT2 expects this.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.agenda import (
    AgendaDetector,
    AgendaReferenceError,
    UNCATEGORIZED_LABEL,
    validate_agenda_references,
)
from spectrum_systems_core.agenda.agenda_detector import (
    build_chunk_to_agenda_mapping,
)


class _StubRegistry:
    def __init__(self, model: str = "claude-sonnet-4-6"):
        self._model = model
        self.calls: List[str] = []

    def get(self, task_type: str) -> Dict[str, str]:
        self.calls.append(task_type)
        return {"model": self._model, "version": "1.0.0"}


def _chunks(n: int, prefix: str = "c") -> List[Dict[str, Any]]:
    return [
        {"chunk_id": f"{prefix}-{i:03d}", "chunk_index": i,
         "source_id": "src", "text": f"chunk {i} body text"}
        for i in range(n)
    ]


def _api_returning(payload: Dict[str, Any]):
    def caller(_prompt: str) -> Dict[str, Any]:
        return {"text": json.dumps(payload)}
    return caller


# ---------------------------------------------------------------------------
# Detection happy path
# ---------------------------------------------------------------------------


def test_detects_two_agenda_items_from_explicit_agenda():
    payload = {
        "agenda_items": [
            {"ordinal": 1, "label": "FSS Protection",
             "approximate_start_chunk_index": 0},
            {"ordinal": 2, "label": "COA Review",
             "approximate_start_chunk_index": 3},
        ],
        "detection_confidence": 0.92,
        "rationale": "Explicit agenda read at turn 0.",
    }
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None, api_caller=_api_returning(payload),
    )
    chunks = _chunks(10)
    result = detector.detect(
        chunks, source_id="src", pipeline_run_id=str(uuid.uuid4()),
    )
    assert result["detection_succeeded"] is True
    assert result["detection_method"] == "llm_detected"
    assert result["items_count"] == 2
    labels = {item["label"] for item in result["agenda_items"]}
    assert "FSS Protection" in labels
    assert "COA Review" in labels
    # Ranges must cover all chunks: item 1 starts at first chunk, last
    # item ends at the last chunk.
    items = sorted(result["agenda_items"], key=lambda d: d["ordinal"])
    assert items[0]["start_turn_id"] == chunks[0]["chunk_id"]
    assert items[-1]["end_turn_id"] == chunks[-1]["chunk_id"]


# ---------------------------------------------------------------------------
# Attack 1: silent success on single generic item
# ---------------------------------------------------------------------------


def test_single_item_treated_as_undetected():
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=_api_returning(
            {"agenda_items": [{"ordinal": 1, "label": "Whole Meeting",
                              "approximate_start_chunk_index": 0}]}
        ),
    )
    result = detector.detect(_chunks(10), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["detection_method"] == "undetected"
    assert result["items_count"] == 1
    assert result["agenda_items"][0]["label"] == UNCATEGORIZED_LABEL


def test_distinct_labels_required_attack_1():
    """Three items, all labelled 'Discussion' -> undetected."""
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=_api_returning({
            "agenda_items": [
                {"ordinal": 1, "label": "Discussion",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "discussion",
                 "approximate_start_chunk_index": 3},
                {"ordinal": 3, "label": "Discussion",
                 "approximate_start_chunk_index": 6},
            ],
        }),
    )
    result = detector.detect(_chunks(10), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["detection_method"] == "undetected"


def test_generic_token_only_labels_rejected():
    """Two distinct labels but both made of purely generic tokens."""
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=_api_returning({
            "agenda_items": [
                {"ordinal": 1, "label": "Meeting",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "Discussion",
                 "approximate_start_chunk_index": 5},
            ],
        }),
    )
    result = detector.detect(_chunks(10), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False


# ---------------------------------------------------------------------------
# Model registry contract (Attack 8)
# ---------------------------------------------------------------------------


def test_uses_generation_task_type():
    registry = _StubRegistry()
    AgendaDetector(registry, sdl_root=None)
    assert registry.calls == ["generation"]


def test_logs_actual_model_used(caplog):
    registry = _StubRegistry(model="claude-sonnet-4-6")
    with caplog.at_level(logging.INFO,
                         logger="spectrum_systems_core.agenda.agenda_detector"):
        AgendaDetector(registry, sdl_root=None)
    assert any(
        "agenda_detector_initialized_with_model" in rec.message
        and "claude-sonnet-4-6" in rec.message
        for rec in caplog.records
    )


def test_artifact_records_actual_model_used():
    """Attack 4: an undetected agenda_item must still carry the model id
    so a new engineer can answer 'which model ran?' from the artifact
    alone.
    """
    registry = _StubRegistry(model="claude-haiku-from-registry")
    detector = AgendaDetector(
        registry, sdl_root=None,
        api_caller=_api_returning({"agenda_items": []}),
    )
    result = detector.detect(_chunks(5), "src", str(uuid.uuid4()))
    assert result["agenda_items"][0]["detector_model_used"] == (
        "claude-haiku-from-registry"
    )


# ---------------------------------------------------------------------------
# Failure paths (Attack 9, API failure)
# ---------------------------------------------------------------------------


def test_api_failure_returns_undetected():
    def boom(_prompt: str) -> Dict[str, Any]:
        raise RuntimeError("api 500")

    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None, api_caller=boom,
    )
    result = detector.detect(_chunks(10), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["detection_method"] == "undetected"
    assert result["items_count"] == 1
    assert result.get("detection_failure_reason") == "api_error"


def test_max_duration_enforced():
    """Attack 9: a slow LLM must not block the pipeline indefinitely.

    Uses a fake clock so the test is deterministic and fast. The clock
    jumps past MAX_DETECTION_DURATION_SECONDS during the api call.
    """
    ticks = iter([0.0, 999.0])  # start, after-call

    def fake_clock():
        return next(ticks)

    def slow_caller(_prompt: str) -> Dict[str, Any]:
        return {"text": json.dumps({
            "agenda_items": [
                {"ordinal": 1, "label": "FSS Protection",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "COA Review",
                 "approximate_start_chunk_index": 5},
            ],
        })}

    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=slow_caller, clock=fake_clock,
    )
    result = detector.detect(_chunks(10), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["detection_method"] == "undetected"
    assert result.get("detection_failure_reason") == "timeout"
    # The detector should report the actual elapsed duration so callers
    # can record / smoke-test it.
    assert result["detection_duration_seconds"] >= (
        AgendaDetector.MAX_DETECTION_DURATION_SECONDS
    )


def test_empty_chunks_returns_no_items():
    detector = AgendaDetector(_StubRegistry(), sdl_root=None)
    result = detector.detect([], "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["agenda_items"] == []
    assert result["detection_method"] == "undetected"


def test_malformed_response_returns_undetected():
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=lambda _p: {"text": "not json at all"},
    )
    result = detector.detect(_chunks(5), "src", str(uuid.uuid4()))
    assert result["detection_succeeded"] is False
    assert result["detection_method"] == "undetected"


# ---------------------------------------------------------------------------
# Artifact validity against the schema
# ---------------------------------------------------------------------------


def test_produced_artifacts_validate_against_schema():
    import json
    import pathlib
    import jsonschema

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parents[2]
         / "contracts" / "schemas" / "agenda"
         / "agenda_item.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=_api_returning({
            "agenda_items": [
                {"ordinal": 1, "label": "FSS Protection",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "COA Review",
                 "approximate_start_chunk_index": 5},
            ],
            "detection_confidence": 0.9,
        }),
    )
    result = detector.detect(
        _chunks(10), source_id="src",
        pipeline_run_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
    )
    for item in result["agenda_items"]:
        validator.validate(item)


def test_undetected_artifact_validates_against_schema():
    import json
    import pathlib
    import jsonschema

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parents[2]
         / "contracts" / "schemas" / "agenda"
         / "agenda_item.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    detector = AgendaDetector(
        _StubRegistry(), sdl_root=None,
        api_caller=lambda _p: {"text": "{}"},
    )
    result = detector.detect(_chunks(5), "src", str(uuid.uuid4()))
    for item in result["agenda_items"]:
        validator.validate(item)


# ---------------------------------------------------------------------------
# build_chunk_to_agenda_mapping + validate_agenda_references
# ---------------------------------------------------------------------------


def test_chunk_to_agenda_mapping_covers_every_chunk():
    chunks = _chunks(10)
    agenda_items = [
        {"agenda_item_id": "aid-1", "start_turn_id": chunks[0]["chunk_id"],
         "end_turn_id": chunks[4]["chunk_id"]},
        {"agenda_item_id": "aid-2", "start_turn_id": chunks[5]["chunk_id"],
         "end_turn_id": chunks[9]["chunk_id"]},
    ]
    mapping = build_chunk_to_agenda_mapping(chunks, agenda_items)
    assert len(mapping) == 10
    assert mapping[chunks[0]["chunk_id"]] == "aid-1"
    assert mapping[chunks[4]["chunk_id"]] == "aid-1"
    assert mapping[chunks[5]["chunk_id"]] == "aid-2"
    assert mapping[chunks[9]["chunk_id"]] == "aid-2"


def test_validate_agenda_references_passes_on_valid_mapping():
    chunks = _chunks(5)
    items = [{"agenda_item_id": "aid-1"}]
    for c in chunks:
        c["agenda_item_id"] = "aid-1"
    validate_agenda_references(chunks, items)  # must not raise


def test_validate_agenda_references_raises_on_dangling_reference():
    """Attack 12 (RT1): a chunk pointing at a non-existent agenda_item
    must halt the pipeline, not silently fall through.
    """
    chunks = _chunks(3)
    for c in chunks:
        c["agenda_item_id"] = "ghost-id"
    with pytest.raises(AgendaReferenceError):
        validate_agenda_references(chunks, [{"agenda_item_id": "aid-1"}])
