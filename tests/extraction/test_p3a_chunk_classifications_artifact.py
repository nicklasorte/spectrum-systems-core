"""Phase P3-A T-2: chunk_classifications aggregate artifact tests."""
from __future__ import annotations

import json
from pathlib import Path

from spectrum_systems_core.extraction.typed_extraction_runner import (
    _build_chunk_classifications_artifact,
    _chunk_classifications_path,
    _write_chunk_classifications_artifact,
)


def _classification(chunk_id: str, classification: str, fb: bool = False) -> dict:
    return {
        "classification_id": "test-cl-" + chunk_id,
        "chunk_id": chunk_id,
        "source_id": "test-source",
        "classification": classification,
        "regulatory_verb_fallback_applied": fb,
        "confidence": 0.9,
        "artifact_type": "chunk_classification",
        "schema_version": "1.0.0",
        "created_at": "1970-01-01T00:00:00+00:00",
    }


def test_artifact_carries_required_fields() -> None:
    artifact = _build_chunk_classifications_artifact(
        source_artifact_id="sa-1",
        source_id="src-1",
        extraction_run_id="run-1",
        extraction_mode="two_stage",
        classifications=[
            _classification("c-1", "decision"),
            _classification("c-2", "claim"),
            _classification("c-3", "off_topic"),
        ],
        extraction_path_breakdown={
            "decision": 1, "claim": 1, "action_item": 0, "off_topic": 1,
        },
        off_topic_rate=1/3,
        router_model="claude-haiku-4-5-20251001",
    )
    assert artifact["artifact_type"] == "chunk_classifications"
    assert artifact["schema_version"] == "1.0.0"
    assert artifact["source_artifact_id"] == "sa-1"
    assert artifact["chunk_count"] == 3
    assert artifact["extraction_mode"] == "two_stage"
    assert artifact["extraction_path_breakdown"]["off_topic"] == 1
    assert artifact["off_topic_skip_count"] == 1
    assert abs(artifact["off_topic_rate"] - 1/3) < 1e-9


def test_path_template_uses_double_underscore_separator() -> None:
    target = _chunk_classifications_path(Path("/tmp"), "sa-1")
    assert target.name == "sa-1_chunk_classifications.json"


def test_write_atomic_and_round_trips(tmp_path: Path) -> None:
    artifact = _build_chunk_classifications_artifact(
        source_artifact_id="sa-1",
        source_id="src-1",
        extraction_run_id="run-1",
        extraction_mode="two_stage",
        classifications=[_classification("c-1", "decision")],
        extraction_path_breakdown={
            "decision": 1, "claim": 0, "action_item": 0, "off_topic": 0,
        },
        off_topic_rate=0.0,
        router_model="claude-haiku-4-5-20251001",
    )
    target = _write_chunk_classifications_artifact(
        artifact, tmp_path, source_artifact_id="sa-1",
    )
    assert target is not None
    assert target.is_file()
    # No leftover .tmp file (atomic rename guarantee).
    assert not target.with_suffix(target.suffix + ".tmp").exists()
    round_trip = json.loads(target.read_text(encoding="utf-8"))
    assert round_trip["source_artifact_id"] == "sa-1"
    assert round_trip["chunk_count"] == 1
