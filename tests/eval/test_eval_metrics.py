"""Tests for EvalMetrics (Phase M.4)."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.evals.m4 import EvalMetrics

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "eval"


def _load_schema(name: str) -> dict:
    return json.loads(
        (CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def _alignment_with(
    coverage_aligns: list[dict],
    review_aligns: list[dict],
    *,
    chunking_strategy: str = "speaker_turn",
    pair_id: str = "11111111-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
    source_artifact_id: str = "src-x",
    minutes_artifact_id: str = "min-x",
) -> dict:
    return {
        "alignment_result_id": "00000000-0000-4000-8000-000000000001",
        "source_artifact_id": source_artifact_id,
        "minutes_artifact_id": minutes_artifact_id,
        "pair_id": pair_id,
        "artifact_type": "alignment_result",
        "schema_version": "1.0.0",
        "created_at": "2026-05-11T00:00:00+00:00",
        "coverage_alignments": coverage_aligns,
        "review_alignments": review_aligns,
        "chunking_strategy": chunking_strategy,
        "provenance": {"produced_by": "EvalAligner"},
    }


def test_coverage_computed_correctly() -> None:
    """3 minutes items, 2 matched -> coverage = 0.667."""
    coverage_aligns = [
        {
            "minutes_item_text": "x",
            "matched_extracted_item_id": "ex-1",
            "matched_extracted_item_text": "y",
            "semantic_similarity": 0.9,
            "content_word_overlap": 3,
            "alignment_status": "matched",
        },
        {
            "minutes_item_text": "x",
            "matched_extracted_item_id": "ex-2",
            "matched_extracted_item_text": "y",
            "semantic_similarity": 0.9,
            "content_word_overlap": 3,
            "alignment_status": "matched",
        },
        {
            "minutes_item_text": "x",
            "matched_extracted_item_id": None,
            "matched_extracted_item_text": None,
            "semantic_similarity": 0.1,
            "content_word_overlap": 0,
            "alignment_status": "unmatched",
        },
    ]
    review_aligns = [
        {
            "extracted_item_id": "ex-1",
            "extracted_item_text": "y",
            "source_turn_ids": [],
            "source_turn_validation": "verified",
            "matched_minutes_text": "x",
            "semantic_similarity": 0.9,
            "alignment_status": "matched",
        }
    ]
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with(coverage_aligns, review_aligns),
        pipeline_run_id="run-1",
        prompt_version="v0",
    )
    assert result["coverage"] == pytest.approx(0.667, abs=0.01)
    assert result["total_minutes_items"] == 3


def test_precision_computed_correctly() -> None:
    """4 extracted items, 3 matched -> precision = 0.75."""
    coverage_aligns = []
    review_aligns = [
        {
            "extracted_item_id": f"ex-{i}",
            "extracted_item_text": "y",
            "source_turn_ids": [],
            "source_turn_validation": "verified",
            "matched_minutes_text": "x" if i < 3 else None,
            "semantic_similarity": 0.9 if i < 3 else 0.2,
            "alignment_status": "matched" if i < 3 else "requires_review",
        }
        for i in range(4)
    ]
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with(coverage_aligns, review_aligns),
        pipeline_run_id="run-1",
        prompt_version="v0",
    )
    assert result["precision"] == pytest.approx(0.75, abs=0.001)
    assert result["total_extracted_items"] == 4


def test_items_requiring_review_count() -> None:
    """2 unmatched extracted items -> items_requiring_review = 2."""
    coverage_aligns = []
    review_aligns = [
        {
            "extracted_item_id": "ex-0",
            "extracted_item_text": "y",
            "source_turn_ids": [],
            "source_turn_validation": "verified",
            "matched_minutes_text": "x",
            "semantic_similarity": 0.9,
            "alignment_status": "matched",
        },
        {
            "extracted_item_id": "ex-1",
            "extracted_item_text": "y",
            "source_turn_ids": [],
            "source_turn_validation": "verified",
            "matched_minutes_text": None,
            "semantic_similarity": 0.2,
            "alignment_status": "requires_review",
        },
        {
            "extracted_item_id": "ex-2",
            "extracted_item_text": "y",
            "source_turn_ids": [],
            "source_turn_validation": "verified",
            "matched_minutes_text": None,
            "semantic_similarity": 0.2,
            "alignment_status": "requires_review",
        },
    ]
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with(coverage_aligns, review_aligns),
        pipeline_run_id="run-1",
        prompt_version="v0",
    )
    assert result["items_requiring_review"] == 2
    # Rate = 2/3.
    assert result["items_requiring_review_rate"] == pytest.approx(2 / 3, abs=0.001)


def test_eval_result_includes_pipeline_run_id() -> None:
    """pipeline_run_id must be threaded through and non-empty."""
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with([], []),
        pipeline_run_id="run-7",
        prompt_version="v0",
    )
    assert result["pipeline_run_id"] == "run-7"
    assert result["prompt_version"] == "v0"


def test_eval_result_schema_validates() -> None:
    """Metrics output validates against the eval_result schema."""
    schema = _load_schema("eval_result")
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with(
            [
                {
                    "minutes_item_text": "x",
                    "matched_extracted_item_id": "ex-1",
                    "matched_extracted_item_text": "y",
                    "semantic_similarity": 0.9,
                    "content_word_overlap": 3,
                    "alignment_status": "matched",
                }
            ],
            [
                {
                    "extracted_item_id": "ex-1",
                    "extracted_item_text": "y",
                    "source_turn_ids": [],
                    "source_turn_validation": "verified",
                    "matched_minutes_text": "x",
                    "semantic_similarity": 0.9,
                    "alignment_status": "matched",
                }
            ],
        ),
        pipeline_run_id="run-x",
        prompt_version="v0",
    )
    jsonschema.Draft202012Validator(schema).validate(result)


def test_zero_extracted_items_does_not_divide_by_zero() -> None:
    """No extracted items -> precision = 0.0, review_rate = 0.0, no exception."""
    metrics = EvalMetrics()
    result = metrics.compute(
        _alignment_with([], []),
        pipeline_run_id="run-1",
        prompt_version="v0",
    )
    assert result["precision"] == 0.0
    assert result["items_requiring_review_rate"] == 0.0
    assert result["coverage"] == 0.0
