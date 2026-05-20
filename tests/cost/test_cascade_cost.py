"""Phase 6 cascade cost estimator tests.

Asserts:
  * `estimate_cascade_cost` returns Decimal and zero for zero items.
  * `estimate_extraction_cost_breakdown` returns extraction +
    cascade + total; cascade is zero when disabled.
  * Pass 2 #4 — estimator within 30% of a synthetic actual on a
    100-item / 30-chunk run.
  * `load_cascade_confirmation_item_threshold` returns 50 by default
    (the value committed in data/cost_constants.json) and the
    fallback (50) when the key is absent from a test constants file.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from spectrum_systems_core.cost.estimator import (
    DEFAULT_CASCADE_PER_CHUNK_OUTPUT_TOKENS,
    CostBreakdown,
    CostConstantsError,
    estimate_cascade_cost,
    estimate_extraction_cost_breakdown,
    load_cascade_confirmation_item_threshold,
    load_cost_constants,
)


def _write_minimal_constants(
    path: Path, *, include_threshold: bool = True
) -> Path:
    body = {
        "artifact_type": "cost_constants",
        "schema_version": "1.0.0",
        "currency": "USD",
        "constants": {
            "claude-haiku-4-7": {
                "input_per_million_tokens": 0.25,
                "output_per_million_tokens": 1.25,
            },
            "claude-sonnet-4-6": {
                "input_per_million_tokens": 3.00,
                "output_per_million_tokens": 15.00,
            },
        },
        "last_updated": "2026-05-20T00:00:00Z",
    }
    if include_threshold:
        body["cascade_confirmation_item_threshold"] = 50
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_estimate_cascade_cost_zero_items() -> None:
    assert estimate_cascade_cost(0) == Decimal("0.000000")


def test_estimate_cascade_cost_returns_decimal() -> None:
    c = estimate_cascade_cost(100)
    assert isinstance(c, Decimal)
    assert c > 0


def test_estimate_breakdown_no_cascade_matches_extraction() -> None:
    b = estimate_extraction_cost_breakdown(
        100 * 1024, "claude-haiku-4-7", enable_cascade=False
    )
    assert isinstance(b, CostBreakdown)
    assert b.cascade_filter_cost == Decimal("0.000000")
    assert b.total_cost == b.extraction_cost


def test_estimate_breakdown_with_cascade_sums() -> None:
    b = estimate_extraction_cost_breakdown(
        100 * 1024,
        "claude-haiku-4-7",
        enable_cascade=True,
        haiku_items_count=100,
    )
    assert b.cascade_filter_cost > Decimal("0")
    assert b.total_cost == b.extraction_cost + b.cascade_filter_cost


def test_load_threshold_default_fifty() -> None:
    """The shipped data/cost_constants.json carries the Phase 6
    threshold value of 50."""
    assert load_cascade_confirmation_item_threshold() == 50


def test_load_threshold_falls_back_to_default_when_absent(
    tmp_path: Path,
) -> None:
    p = _write_minimal_constants(
        tmp_path / "c.json", include_threshold=False
    )
    # Helper does not accept `constants_path` directly; we patch by
    # passing through `load_cost_constants` which the helper uses.
    assert load_cascade_confirmation_item_threshold(p) == 50


# ---------------------------------------------------------------------------
# Pass 2 #4 — within 30% of synthetic actual.
# ---------------------------------------------------------------------------


def test_estimator_within_30pct_of_synthetic_actual(tmp_path: Path) -> None:
    """The estimator predicts cost for a 100-item / 30-chunk cascade.
    We compare to a synthetic 'actual' computed at the more accurate
    3.5 bytes/token ratio (the estimator uses 4 to stay conservative).
    Bound: <= 30%."""
    p = _write_minimal_constants(tmp_path / "c.json")
    chunks = 30
    avg_chunk_text_bytes = 2000
    per_chunk_output_tokens = DEFAULT_CASCADE_PER_CHUNK_OUTPUT_TOKENS

    estimate = estimate_cascade_cost(
        100,
        chunk_count=chunks,
        avg_chunk_text_bytes=avg_chunk_text_bytes,
        per_chunk_output_tokens=per_chunk_output_tokens,
        filter_model="claude-sonnet-4-6",
        constants_path=p,
    )

    # Synthetic actual: 3.5 bytes/token input + same output budget.
    actual_input_tokens = (avg_chunk_text_bytes / 3.5) * chunks
    actual_output_tokens = per_chunk_output_tokens * chunks
    actual_float = (
        actual_input_tokens * 3.00 / 1_000_000
        + actual_output_tokens * 15.00 / 1_000_000
    )
    actual = Decimal(str(round(actual_float, 6)))
    rel = abs(estimate - actual) / actual
    assert rel <= Decimal("0.30"), (
        f"estimator {estimate} differs from synthetic actual {actual} "
        f"by {rel * 100}%; > 30% tolerance"
    )


def test_unknown_filter_model_rejected(tmp_path: Path) -> None:
    p = _write_minimal_constants(tmp_path / "c.json")
    with pytest.raises(CostConstantsError):
        estimate_cascade_cost(
            10,
            filter_model="claude-ghost-1-0",
            constants_path=p,
        )


def test_negative_items_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        estimate_cascade_cost(-1)
