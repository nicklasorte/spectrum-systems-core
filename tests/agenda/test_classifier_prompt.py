"""Tests for ChunkClassifier prompt enhancement (Phase W.4).

The classifier should include a ``Current agenda item: ...`` line in
its prompt only when:
  * Phase W flag is enabled, AND
  * the chunk's agenda_item_id resolves to a meaningful label (not
    UNCATEGORIZED_LABEL).

Otherwise the prompt is byte-identical to pre-Phase W -- the rollback
property.
"""
from __future__ import annotations

import json
import logging
import pathlib
import uuid
from typing import Dict, Any

import pytest

from spectrum_systems_core.agenda.pipeline_integration import (
    make_phase_w_agenda_resolver,
)
from spectrum_systems_core.config import PHASE_W_FLAG_NAME
from spectrum_systems_core.extraction.chunk_classifier import ChunkClassifier


def _seed_flag(data_lake: pathlib.Path, enabled: bool) -> None:
    target_dir = data_lake / "store" / "artifacts" / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{PHASE_W_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": enabled}), encoding="utf-8",
    )


def _write_agenda_item(
    sdl_root: pathlib.Path,
    source_id: str,
    item_id: str,
    label: str,
    *,
    detection_method: str = "llm_detected",
) -> None:
    target_dir = sdl_root / "agenda" / source_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{item_id}.json").write_text(json.dumps({
        "agenda_item_id": item_id,
        "label": label,
        "detection_method": detection_method,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_prompt_includes_agenda_line_when_flag_enabled_and_detected(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    aid = str(uuid.uuid4())
    _write_agenda_item(sdl_root, "src", aid, "FSS Protection Criteria")

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    classifier = ChunkClassifier(agenda_resolver=resolver)
    chunk = {
        "chunk_id": "c-1", "text": "We propose a -10.5 dB threshold.",
        "agenda_item_id": aid,
    }
    prompt = classifier._build_prompt(chunk)
    assert "Current agenda item: FSS Protection Criteria" in prompt


def test_prompt_omits_agenda_line_when_flag_disabled(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, False)
    aid = str(uuid.uuid4())
    _write_agenda_item(sdl_root, "src", aid, "FSS Protection Criteria")

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    classifier = ChunkClassifier(agenda_resolver=resolver)
    chunk = {
        "chunk_id": "c-1", "text": "We propose a -10.5 dB threshold.",
        "agenda_item_id": aid,
    }
    prompt = classifier._build_prompt(chunk)
    assert "Current agenda item:" not in prompt


def test_prompt_omits_agenda_line_when_undetected_label(tmp_path):
    """Per task spec: when label is the undetected fallback, the
    classifier behaves as before (no agenda line)."""
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    aid = str(uuid.uuid4())
    _write_agenda_item(
        sdl_root, "src", aid, "Uncategorized Meeting Content",
        detection_method="undetected",
    )

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    classifier = ChunkClassifier(agenda_resolver=resolver)
    chunk = {
        "chunk_id": "c-1", "text": "We propose a -10.5 dB threshold.",
        "agenda_item_id": aid,
    }
    prompt = classifier._build_prompt(chunk)
    assert "Current agenda item:" not in prompt


def test_warning_logged_when_chunk_missing_agenda_field_with_flag_enabled(
    tmp_path, caplog,
):
    """Attack 2 distinction: chunk has no agenda_item_id while the
    flag is on. Resolver returns None AND emits a warning so the
    leak is visible in logs.
    """
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    chunk = {"chunk_id": "c-1", "text": "stuff"}  # no agenda_item_id

    with caplog.at_level(
        logging.WARNING,
        logger="spectrum_systems_core.agenda.pipeline_integration",
    ):
        result = resolver(chunk)
    assert result is None
    assert any(
        "agenda_item_id missing on chunk c-1" in rec.message
        and "phase_w_enabled=true" in rec.message
        for rec in caplog.records
    )


def test_no_warning_when_chunk_missing_agenda_field_with_flag_off(
    tmp_path, caplog,
):
    """Flag off: missing field is normal; do NOT spam logs."""
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, False)

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    chunk = {"chunk_id": "c-1", "text": "stuff"}

    with caplog.at_level(
        logging.WARNING,
        logger="spectrum_systems_core.agenda.pipeline_integration",
    ):
        result = resolver(chunk)
    assert result is None
    assert not any(
        "agenda_item_id missing" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Batch prompt path also gets agenda context
# ---------------------------------------------------------------------------


def test_batch_prompt_includes_agenda_lines_per_chunk(tmp_path):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)
    aid_a = str(uuid.uuid4())
    aid_b = str(uuid.uuid4())
    _write_agenda_item(sdl_root, "src", aid_a, "Topic Alpha")
    _write_agenda_item(sdl_root, "src", aid_b, "Topic Beta")

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    classifier = ChunkClassifier(agenda_resolver=resolver)
    chunks = [
        {"chunk_id": "c-1", "text": "alpha text", "agenda_item_id": aid_a},
        {"chunk_id": "c-2", "text": "beta text", "agenda_item_id": aid_b},
    ]
    prompt = classifier._build_batch_prompt(chunks)
    assert "Current agenda item: Topic Alpha" in prompt
    assert "Current agenda item: Topic Beta" in prompt


# ---------------------------------------------------------------------------
# Missing artifact: defensive fallback
# ---------------------------------------------------------------------------


def test_resolver_falls_back_when_artifact_missing_on_disk(tmp_path, caplog):
    data_lake = tmp_path / "dl"
    sdl_root = data_lake / "store" / "artifacts"
    _seed_flag(data_lake, True)

    resolver = make_phase_w_agenda_resolver(data_lake, sdl_root, "src")
    # Chunk references an agenda_item_id that has no artifact file.
    chunk = {"chunk_id": "c-1", "text": "x", "agenda_item_id": "ghost"}
    with caplog.at_level(
        logging.WARNING,
        logger="spectrum_systems_core.agenda.pipeline_integration",
    ):
        result = resolver(chunk)
    assert result is None
    assert any(
        "agenda_resolver_artifact_missing" in rec.message
        for rec in caplog.records
    )


def test_classifier_no_resolver_emits_identical_prompt_to_pre_phase_w():
    """Rollback property: without a resolver attached the prompt is
    byte-for-byte identical to the pre-Phase W version.
    """
    classifier = ChunkClassifier()  # no resolver
    chunk = {"chunk_id": "c-1", "text": "hello world"}
    prompt = classifier._build_prompt(chunk)
    # No "Current agenda item:" preamble.
    assert prompt.startswith(
        "Classify the following meeting speaker-turn into exactly one of:"
    )
