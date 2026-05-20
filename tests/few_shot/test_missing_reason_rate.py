"""Phase 3P missing-reason-rate diagnostic tests.

The `few_shot_reason_missing_rate` field on the
``pipeline_invocation_log`` records the fraction of object-form
decisions+action_items that omitted the prompt-required ``reason``.
The diagnostic is logged only when the rate exceeds the warning
threshold (0.20).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spectrum_systems_core.few_shot.loader import count_missing_reason_rate
from spectrum_systems_core.pipeline.governed_run import (
    ExtractionConfig,
    write_pipeline_invocation_log,
)


def test_all_items_carry_reason_rate_zero() -> None:
    payload = {
        "decisions": [
            {"text": "x", "reason": "explicit"},
            {"text": "y", "reason": "implicit"},
        ],
        "action_items": [
            {"action": "post", "reason": "procedural"},
        ],
    }
    assert count_missing_reason_rate(payload) == 0.0


def test_half_items_missing_reason_rate_one_half() -> None:
    payload = {
        "decisions": [
            {"text": "x", "reason": "explicit"},
            {"text": "y"},
        ],
        "action_items": [
            {"action": "post"},
            {"action": "post2", "reason": "procedural"},
        ],
    }
    assert count_missing_reason_rate(payload) == 0.5


def test_legacy_string_items_excluded_from_denominator() -> None:
    payload = {
        "decisions": [
            "a legacy verbatim string",
            "another legacy string",
        ],
        "action_items": [],
    }
    # Legacy string items pre-date the field and are excluded.
    assert count_missing_reason_rate(payload) == 0.0


def test_diagnostic_recorded_only_above_threshold(tmp_path: Path) -> None:
    cfg = ExtractionConfig(
        temperature=0.0,
        seed_inputs={
            "model_id": "claude-haiku-4-5-20251001",
            "prompt_content_hash": "p" * 64,
            "transcript_hash": "t" * 64,
        },
        chunks_full_hash="c" * 64,
        chunk_count=1,
        first_chunk_hash="f" * 64,
        last_chunk_hash="l" * 64,
        prompt_content_hash="p" * 64,
    )
    # Below threshold (0.10 < 0.20): the field is omitted from the log.
    log_below = write_pipeline_invocation_log(
        data_lake_path=tmp_path,
        source_id="src1",
        invocation_id="a" * 32,
        started_at="2026-05-19T10:00:00+00:00",
        completed_at="2026-05-19T10:01:00+00:00",
        caller="production_cli",
        extraction_config=cfg,
        comparison_artifact_path=None,
        few_shot_reason_missing_rate=0.10,
    )
    assert "few_shot_reason_missing_rate" not in log_below

    # Above threshold (0.30 > 0.20): the field IS present.
    log_above = write_pipeline_invocation_log(
        data_lake_path=tmp_path,
        source_id="src2",
        invocation_id="b" * 32,
        started_at="2026-05-19T10:00:00+00:00",
        completed_at="2026-05-19T10:01:00+00:00",
        caller="production_cli",
        extraction_config=cfg,
        comparison_artifact_path=None,
        few_shot_reason_missing_rate=0.30,
    )
    assert "few_shot_reason_missing_rate" in log_above
    assert log_above["few_shot_reason_missing_rate"] == pytest.approx(0.30)


def test_diagnostic_omitted_when_none_passed(tmp_path: Path) -> None:
    cfg = ExtractionConfig(
        temperature=0.0,
        seed_inputs={
            "model_id": "claude-haiku-4-5-20251001",
            "prompt_content_hash": "p" * 64,
            "transcript_hash": "t" * 64,
        },
        chunks_full_hash="c" * 64,
        chunk_count=1,
        first_chunk_hash="f" * 64,
        last_chunk_hash="l" * 64,
        prompt_content_hash="p" * 64,
    )
    log = write_pipeline_invocation_log(
        data_lake_path=tmp_path,
        source_id="src",
        invocation_id="c" * 32,
        started_at="2026-05-19T10:00:00+00:00",
        completed_at="2026-05-19T10:01:00+00:00",
        caller="production_cli",
        extraction_config=cfg,
        comparison_artifact_path=None,
    )
    assert "few_shot_reason_missing_rate" not in log
