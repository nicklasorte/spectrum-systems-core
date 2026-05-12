"""Tests for per-agenda-item slice metrics (Phase W.5).

Each test uses real ``compute_per_agenda_item_metrics`` /
``compute_agenda_stratification`` on hand-built alignment_result dicts so
the minimum-size policy (Attack 10), std-dev computation, and hidden-
stratification flag are exercised end-to-end -- not in isolation.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

import jsonschema
import pytest

from spectrum_systems_core.evals.m4.aligner import (
    HIDDEN_STRATIFICATION_STD_THRESHOLD,
    MIN_CHUNKS_PER_AGENDA,
    compute_agenda_stratification,
    compute_per_agenda_item_metrics,
)


def _chunks_for_agendas(
    sizes_by_agenda: Dict[str, int],
) -> List[Dict[str, Any]]:
    """sizes_by_agenda = {agenda_id: chunk_count}."""
    chunks: List[Dict[str, Any]] = []
    counter = 0
    for aid, n in sizes_by_agenda.items():
        for _ in range(n):
            chunks.append({
                "chunk_id": f"c-{counter:03d}",
                "agenda_item_id": aid,
            })
            counter += 1
    return chunks


def _alignment_result(
    review: List[Dict[str, Any]], coverage: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "review_alignments": review,
        "coverage_alignments": coverage,
    }


def _agenda_items(*ids: str) -> List[Dict[str, Any]]:
    return [{"agenda_item_id": aid} for aid in ids]


# ---------------------------------------------------------------------------
# Per-agenda computation
# ---------------------------------------------------------------------------


def test_per_agenda_metrics_computed():
    """Agenda A (6 chunks, 5 review entries, 4 matched) vs Agenda B
    (5 chunks, 3 review entries, 1 matched). Both above min-size.
    """
    chunks = _chunks_for_agendas({"aid-A": 6, "aid-B": 5})
    review = [
        # 5 review entries from agenda A chunks
        {"extracted_item_id": "ext-A1", "source_turn_ids": ["c-000"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-A2", "source_turn_ids": ["c-001"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-A3", "source_turn_ids": ["c-002"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-A4", "source_turn_ids": ["c-003"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-A5", "source_turn_ids": ["c-004"],
         "alignment_status": "requires_review"},
        # 3 review entries from agenda B chunks (c-006..c-008)
        {"extracted_item_id": "ext-B1", "source_turn_ids": ["c-006"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-B2", "source_turn_ids": ["c-007"],
         "alignment_status": "requires_review"},
        {"extracted_item_id": "ext-B3", "source_turn_ids": ["c-008"],
         "alignment_status": "requires_review"},
    ]
    coverage = [
        {"matched_extracted_item_id": "ext-A1", "alignment_status": "matched"},
        {"matched_extracted_item_id": "ext-A2", "alignment_status": "matched"},
        {"matched_extracted_item_id": "ext-B1", "alignment_status": "matched"},
        {"matched_extracted_item_id": "ext-A3", "alignment_status": "unmatched"},
    ]
    out = compute_per_agenda_item_metrics(
        _alignment_result(review, coverage),
        chunks,
        _agenda_items("aid-A", "aid-B"),
    )
    assert out["agenda_items_evaluated_count"] == 2
    assert out["excluded_small_agenda_count"] == 0
    # Precision: A = 4/5 = 0.8; B = 1/3 ≈ 0.333
    assert out["precision_by_agenda_item"]["aid-A"] == pytest.approx(0.8)
    assert out["precision_by_agenda_item"]["aid-B"] == pytest.approx(1/3)
    # Coverage (per spec definition):
    #   aid-A: 3 cov entries pointed at A; 2 matched -> 2/3
    #   aid-B: 1 cov entry; 1 matched -> 1.0
    assert out["coverage_by_agenda_item"]["aid-A"] == pytest.approx(2/3)
    assert out["coverage_by_agenda_item"]["aid-B"] == pytest.approx(1.0)


def test_small_agenda_excluded_from_per_slice():
    """Attack 10: an agenda with 3 chunks (<MIN_CHUNKS_PER_AGENDA=5) is
    reported as 'excluded_small', not assigned a fragile metric.
    """
    assert MIN_CHUNKS_PER_AGENDA == 5  # ensure constant unchanged
    chunks = _chunks_for_agendas({"aid-big": 5, "aid-tiny": 3})
    review = [
        # 3 from big, 2 from tiny
        {"extracted_item_id": "ext-1", "source_turn_ids": ["c-000"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-2", "source_turn_ids": ["c-001"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-3", "source_turn_ids": ["c-002"],
         "alignment_status": "requires_review"},
        # tiny agenda items -- should be excluded
        {"extracted_item_id": "ext-4", "source_turn_ids": ["c-005"],
         "alignment_status": "matched"},
        {"extracted_item_id": "ext-5", "source_turn_ids": ["c-006"],
         "alignment_status": "matched"},
    ]
    out = compute_per_agenda_item_metrics(
        _alignment_result(review, []),
        chunks,
        _agenda_items("aid-big", "aid-tiny"),
    )
    assert out["excluded_small_agenda_count"] == 1
    assert out["agenda_items_evaluated_count"] == 1
    assert out["precision_by_agenda_item"]["aid-tiny"] == "excluded_small"
    assert out["coverage_by_agenda_item"]["aid-tiny"] == "excluded_small"
    assert out["precision_by_agenda_item"]["aid-big"] == pytest.approx(2/3)


def test_excluded_small_string_is_exactly_excluded_small():
    """RT3: the marker value must be the specific sentinel string the
    eval_result schema expects, not 'small' or 'tiny'."""
    chunks = _chunks_for_agendas({"aid-tiny": 1})
    out = compute_per_agenda_item_metrics(
        _alignment_result([], []), chunks, _agenda_items("aid-tiny"),
    )
    assert out["precision_by_agenda_item"]["aid-tiny"] == "excluded_small"


def test_agenda_with_no_alignments_reports_zero_not_excluded():
    """An evaluable agenda (>=5 chunks) with no review_alignments
    should report 0.0, not silently get excluded as small."""
    chunks = _chunks_for_agendas({"aid-orphan": 6})
    out = compute_per_agenda_item_metrics(
        _alignment_result([], []), chunks, _agenda_items("aid-orphan"),
    )
    assert out["precision_by_agenda_item"]["aid-orphan"] == 0.0
    assert out["coverage_by_agenda_item"]["aid-orphan"] == 0.0


# ---------------------------------------------------------------------------
# Std-deviation + hidden stratification flag
# ---------------------------------------------------------------------------


def test_std_deviation_computed_across_agendas():
    """Three agendas with widely varying precision -> non-zero std-dev."""
    per_agenda = {
        "coverage_by_agenda_item": {
            "a": 0.9, "b": 0.5, "c": 0.1,
        },
        "precision_by_agenda_item": {
            "a": 0.9, "b": 0.5, "c": 0.1,
        },
        "excluded_small_agenda_count": 0,
        "agenda_items_evaluated_count": 3,
    }
    out = compute_agenda_stratification(per_agenda)
    # Population std-dev of [0.1, 0.5, 0.9] = sqrt(((0.4)^2 + 0 + (0.4)^2)/3)
    expected = ((0.4 ** 2 + 0 + 0.4 ** 2) / 3) ** 0.5
    assert out["agenda_coverage_std_deviation"] == pytest.approx(expected)
    assert out["agenda_precision_std_deviation"] == pytest.approx(expected)


def test_hidden_stratification_flag_set_when_high_variance():
    per_agenda = {
        "coverage_by_agenda_item": {"a": 0.95, "b": 0.10},
        "precision_by_agenda_item": {"a": 0.9, "b": 0.85},
    }
    out = compute_agenda_stratification(per_agenda)
    # Coverage std-dev across [0.95, 0.10] is ~0.425 > 0.20.
    assert out["agenda_coverage_std_deviation"] > 0.20
    assert out["flag_hidden_stratification"] is True


def test_hidden_stratification_flag_not_set_when_low_variance():
    per_agenda = {
        "coverage_by_agenda_item": {"a": 0.81, "b": 0.79, "c": 0.80},
        "precision_by_agenda_item": {"a": 0.7, "b": 0.72},
    }
    out = compute_agenda_stratification(per_agenda)
    assert out["agenda_coverage_std_deviation"] < 0.05
    assert out["flag_hidden_stratification"] is False


def test_std_deviation_zero_when_fewer_than_two_agendas():
    """Only one evaluable agenda -> no spread to measure."""
    per_agenda = {
        "coverage_by_agenda_item": {"a": 0.5},
        "precision_by_agenda_item": {"a": 0.5},
    }
    out = compute_agenda_stratification(per_agenda)
    assert out["agenda_coverage_std_deviation"] == 0.0
    assert out["flag_hidden_stratification"] is False


def test_excluded_strings_excluded_from_std_dev():
    """An agenda marked 'excluded_small' must not be counted in std-dev."""
    per_agenda = {
        "coverage_by_agenda_item": {"a": 0.5, "b": 0.6, "c": "excluded_small"},
        "precision_by_agenda_item": {"a": 0.5, "b": 0.6, "c": "excluded_small"},
    }
    out = compute_agenda_stratification(per_agenda)
    # std-dev across {0.5, 0.6} = 0.05 (not influenced by the excluded entry).
    assert out["agenda_coverage_std_deviation"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------


def test_eval_result_schema_accepts_new_phase_w_fields():
    schema_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "contracts" / "schemas" / "eval" / "eval_result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    base = {
        "eval_result_id": "00000000-0000-0000-0000-000000000001",
        "alignment_result_id": "00000000-0000-0000-0000-000000000002",
        "source_artifact_id": "src",
        "minutes_artifact_id": "min",
        "pair_id": "00000000-0000-0000-0000-000000000003",
        "pipeline_run_id": "run-1",
        "prompt_version": "v1",
        "artifact_type": "eval_result",
        "schema_version": "1.1.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "chunking_strategy": "speaker_turn",
        "coverage": 0.8,
        "precision": 0.7,
        "items_requiring_review": 0,
        "items_requiring_review_rate": 0.0,
        "total_extracted_items": 5,
        "total_minutes_items": 6,
        "coverage_by_agenda_item": {"aid-1": 0.8, "aid-2": "excluded_small"},
        "precision_by_agenda_item": {"aid-1": 0.7, "aid-2": "excluded_small"},
        "excluded_small_agenda_count": 1,
        "agenda_items_evaluated_count": 1,
        "provenance": {"produced_by": "EvalMetrics"},
    }
    validator.validate(base)


def test_eval_result_schema_still_accepts_old_v1_0_0_artifacts():
    """Rollback compatibility: a pre-Phase W eval_result must still
    validate against the bumped schema.
    """
    schema_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "contracts" / "schemas" / "eval" / "eval_result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    old = {
        "eval_result_id": "00000000-0000-0000-0000-000000000001",
        "alignment_result_id": "00000000-0000-0000-0000-000000000002",
        "source_artifact_id": "src",
        "minutes_artifact_id": "min",
        "pair_id": "00000000-0000-0000-0000-000000000003",
        "pipeline_run_id": "run-1",
        "prompt_version": "v1",
        "artifact_type": "eval_result",
        "schema_version": "1.0.0",  # old version
        "created_at": "2026-05-12T00:00:00+00:00",
        "chunking_strategy": "speaker_turn",
        "coverage": 0.8,
        "precision": 0.7,
        "items_requiring_review": 0,
        "items_requiring_review_rate": 0.0,
        "total_extracted_items": 5,
        "total_minutes_items": 6,
        "provenance": {"produced_by": "EvalMetrics"},
    }
    jsonschema.Draft202012Validator(schema).validate(old)


def test_eval_summary_schema_accepts_phase_w_hidden_stratification():
    schema_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "contracts" / "schemas" / "eval" / "eval_summary.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = {
        "eval_summary_id": "00000000-0000-0000-0000-000000000099",
        "pipeline_run_id": "run-1",
        "artifact_type": "eval_summary",
        "schema_version": "1.2.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "pairs_evaluated": 1,
        "pairs_skipped_pending_review": 0,
        "aggregate_coverage": 0.8,
        "aggregate_precision": 0.7,
        "total_items_requiring_review": 0,
        "by_chunking_strategy": {
            "speaker_turn": {"coverage": 0.8, "precision": 0.7, "pairs_count": 1},
            "character_count_fallback": {
                "coverage": 0.0, "precision": 0.0, "pairs_count": 0,
            },
        },
        "eval_results": [],
        "is_baseline": False,
        "baseline_eval_summary_id": None,
        "regression_detected": False,
        "regression_detail": [],
        "partial_run_warning": False,
        "partial_run_detail": None,
        "agenda_coverage_std_deviation": 0.05,
        "agenda_precision_std_deviation": 0.10,
        "flag_hidden_stratification": False,
        "provenance": {"produced_by": "EvalRunner"},
    }
    jsonschema.Draft202012Validator(schema).validate(payload)
