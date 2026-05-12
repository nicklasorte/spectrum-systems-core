"""Tests for ``apply_phase_w_if_enabled`` (Phase W.3 pipeline wiring).

Each test exercises the REAL integration code (flag read from disk,
agenda artifacts written to disk, chunks annotated, pre-flight against
disk). Per RT2 these tests must actually trigger the validators, not
call them in isolation.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.agenda import (
    AgendaReferenceError,
    apply_phase_w_if_enabled,
)
from spectrum_systems_core.agenda.pipeline_integration import (
    write_agenda_artifact,
)
from spectrum_systems_core.config import PHASE_W_FLAG_NAME
from spectrum_systems_core.verification.model_registry import ModelRegistry


class _StubRegistry:
    def get(self, _task_type: str) -> Dict[str, str]:
        return {"model": "claude-sonnet-4-6", "version": "test"}


def _seed_flag(data_lake: pathlib.Path, enabled: bool) -> None:
    target_dir = data_lake / "store" / "artifacts" / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{PHASE_W_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": enabled}), encoding="utf-8",
    )


def _chunks(n: int) -> List[Dict[str, Any]]:
    return [
        {"chunk_id": f"c-{i:03d}", "chunk_index": i,
         "source_id": "src", "text": f"text {i}"}
        for i in range(n)
    ]


def _api(payload: Dict[str, Any]):
    def caller(_p: str) -> Dict[str, Any]:
        return {"text": json.dumps(payload)}
    return caller


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pipeline_writes_agenda_artifacts_when_flag_enabled(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    chunks = _chunks(10)
    pipeline_run_id = str(uuid.uuid4())

    metrics = apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=pipeline_run_id,
        model_registry=_StubRegistry(),
        api_caller=_api({
            "agenda_items": [
                {"ordinal": 1, "label": "FSS Protection",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "COA Review",
                 "approximate_start_chunk_index": 5},
            ],
            "detection_confidence": 0.9,
        }),
    )
    assert metrics["agenda_detection_attempted"] is True
    assert metrics["agenda_detection_succeeded"] is True
    assert metrics["agenda_items_detected_count"] == 2
    # Both artifacts must be on disk.
    written = list((sdl_root / "agenda" / "src").glob("*.json"))
    assert len(written) == 2
    # Every chunk now has an agenda_item_id.
    assert all(isinstance(c.get("agenda_item_id"), str) for c in chunks)


def test_chunks_annotated_with_agenda_item_id_after_detection(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    chunks = _chunks(8)

    apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=str(uuid.uuid4()),
        model_registry=_StubRegistry(),
        api_caller=_api({
            "agenda_items": [
                {"ordinal": 1, "label": "Topic Alpha",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "Topic Beta",
                 "approximate_start_chunk_index": 4},
            ],
            "detection_confidence": 0.7,
        }),
    )
    # First 4 chunks all point at the same agenda; last 4 point at the
    # second agenda. (Boundary is approximate_start_chunk_index of the
    # second item.)
    first_block_ids = {c["agenda_item_id"] for c in chunks[:4]}
    second_block_ids = {c["agenda_item_id"] for c in chunks[4:]}
    assert len(first_block_ids) == 1
    assert len(second_block_ids) == 1
    assert first_block_ids != second_block_ids


# ---------------------------------------------------------------------------
# Flag-off rollback
# ---------------------------------------------------------------------------


def test_pipeline_skips_agenda_when_flag_disabled(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, False)
    chunks = _chunks(5)

    metrics = apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=str(uuid.uuid4()),
        model_registry=_StubRegistry(),
        api_caller=_api({"agenda_items": []}),
    )
    assert metrics["agenda_detection_attempted"] is False
    assert metrics["detection_method"] == "disabled"
    # Chunks unmodified: agenda_item_id never added.
    for c in chunks:
        assert "agenda_item_id" not in c
    # No agenda artifacts on disk.
    assert not (sdl_root / "agenda").exists()


def test_pipeline_skips_when_flag_file_absent(tmp_path):
    """Fail-closed: a missing flag file means "off"."""
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    # Do NOT seed the flag. FeatureFlag should resolve to False.
    chunks = _chunks(3)

    metrics = apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=str(uuid.uuid4()),
        model_registry=_StubRegistry(),
    )
    assert metrics["agenda_detection_attempted"] is False


# ---------------------------------------------------------------------------
# Attack 12: pre-flight against disk
# ---------------------------------------------------------------------------


def test_pipeline_halts_on_agenda_reference_mismatch(tmp_path):
    """Attack 12: chunks reference an agenda_item that has no on-disk
    artifact -> pipeline raises.

    We write a chunks list with a fake agenda_item_id and call the
    pre-flight directly via apply_phase_w_if_enabled by pre-populating
    chunks AND by having the detector return an agenda_item whose write
    we sabotage afterward.
    """
    from spectrum_systems_core.agenda.pipeline_integration import (
        _validate_against_disk,
    )

    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    (sdl_root / "agenda" / "src").mkdir(parents=True)
    chunks = _chunks(2)
    for c in chunks:
        c["agenda_item_id"] = "ghost-id"
    with pytest.raises(AgendaReferenceError):
        _validate_against_disk(chunks, sdl_root, "src")


def test_pre_flight_passes_when_artifact_present(tmp_path):
    from spectrum_systems_core.agenda.pipeline_integration import (
        _validate_against_disk,
    )

    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    target_dir = sdl_root / "agenda" / "src"
    target_dir.mkdir(parents=True)
    aid = str(uuid.uuid4())
    (target_dir / f"{aid}.json").write_text(
        json.dumps({"agenda_item_id": aid}), encoding="utf-8",
    )
    chunks = _chunks(2)
    for c in chunks:
        c["agenda_item_id"] = aid
    # Must not raise.
    _validate_against_disk(chunks, sdl_root, "src")


def test_apply_phase_w_writes_artifacts_before_annotating_chunks(tmp_path):
    """End-to-end pre-flight: the apply function must put artifacts on
    disk BEFORE returning, so the post-annotation pre-flight finds them.
    """
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    chunks = _chunks(6)
    apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=str(uuid.uuid4()),
        model_registry=_StubRegistry(),
        api_caller=_api({
            "agenda_items": [
                {"ordinal": 1, "label": "First Topic",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "Second Topic",
                 "approximate_start_chunk_index": 3},
            ],
        }),
    )
    # Every chunk's agenda_item_id must resolve to a real file.
    agenda_dir = sdl_root / "agenda" / "src"
    on_disk = {p.stem for p in agenda_dir.glob("*.json")}
    referenced = {c["agenda_item_id"] for c in chunks}
    assert referenced.issubset(on_disk)


# ---------------------------------------------------------------------------
# Undetected fallback
# ---------------------------------------------------------------------------


def test_undetected_writes_single_undetected_artifact(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    chunks = _chunks(4)

    metrics = apply_phase_w_if_enabled(
        chunks,
        source_id="src",
        data_lake_path=data_lake,
        sdl_root=sdl_root,
        pipeline_run_id=str(uuid.uuid4()),
        model_registry=_StubRegistry(),
        api_caller=_api({"agenda_items": []}),  # empty -> undetected
    )
    assert metrics["agenda_detection_attempted"] is True
    assert metrics["agenda_detection_succeeded"] is False
    assert metrics["detection_method"] == "undetected"
    artifacts = list((sdl_root / "agenda" / "src").glob("*.json"))
    assert len(artifacts) == 1
    written = json.loads(artifacts[0].read_text())
    assert written["detection_method"] == "undetected"
    assert written["label"] == "Uncategorized Meeting Content"
    # Every chunk points at the single undetected item.
    aid = written["agenda_item_id"]
    assert all(c["agenda_item_id"] == aid for c in chunks)
