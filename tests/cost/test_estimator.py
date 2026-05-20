"""Phase 4 — cost estimator tests.

Pass 1 #8 (sanity), Pass 2 #4 (mock-vs-actual within 30%), and
Pass 3 #6 (bound enforcement) live here.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from spectrum_systems_core.cost.estimator import (
    DEFAULT_HAIKU_OUTPUT_TOKENS,
    DEFAULT_OPUS_OUTPUT_TOKENS,
    CostConstantsError,
    estimate_extraction_cost,
    load_cost_constants,
)


def _write_constants(
    path: Path,
    *,
    haiku_in: float = 0.25,
    haiku_out: float = 1.25,
    opus_in: float = 15.00,
    opus_out: float = 75.00,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "artifact_type": "cost_constants",
                "schema_version": "1.0.0",
                "currency": "USD",
                "constants": {
                    "claude-haiku-4-7": {
                        "input_per_million_tokens": haiku_in,
                        "output_per_million_tokens": haiku_out,
                    },
                    "claude-opus-4-7": {
                        "input_per_million_tokens": opus_in,
                        "output_per_million_tokens": opus_out,
                    },
                },
                "last_updated": "2026-05-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Pass 1 #8 — order-of-magnitude sanity.
# ---------------------------------------------------------------------------


def test_100kb_through_haiku_is_in_cents(tmp_path: Path) -> None:
    """100KB through Haiku should be a small number of cents."""
    p = _write_constants(tmp_path / "c.json")
    cost = estimate_extraction_cost(100 * 1024, "claude-haiku-4-7", constants_path=p)
    # 25600 input tokens * $0.25/M + 2000 output * $1.25/M = $0.0064 + $0.0025
    # = $0.0089. Bound the sanity test loosely so a future tweak does
    # not silently rip out the order-of-magnitude check.
    assert Decimal("0.005") <= cost <= Decimal("0.02")


def test_100kb_through_opus_is_in_dollars(tmp_path: Path) -> None:
    """100KB through Opus should be on the order of $1."""
    p = _write_constants(tmp_path / "c.json")
    cost = estimate_extraction_cost(100 * 1024, "claude-opus-4-7", constants_path=p)
    # 25600 input tokens * $15/M + 10000 output * $75/M = $0.384 + $0.75 = $1.134
    assert Decimal("0.5") <= cost <= Decimal("2.5")


# ---------------------------------------------------------------------------
# Pass 2 #4 — estimator vs. mock-recorded actual within 30%.
# ---------------------------------------------------------------------------


def test_estimator_within_30pct_of_known_actual(tmp_path: Path) -> None:
    """A synthetic 'actual' usage is recorded via the same arithmetic
    the API client would emit. The estimator's output is asserted to
    fall within 30% of that synthetic actual — the practical bound for
    a token-based estimator (a real average for English is ~3.5
    bytes/token; the estimator's 4.0 systematically under-counts
    input tokens, so the relative error is < 15% for any real
    transcript)."""
    p = _write_constants(tmp_path / "c.json")
    byte_len = 100 * 1024  # 100KB
    out_tokens = 2000

    estimate = estimate_extraction_cost(
        byte_len, "claude-haiku-4-7", constants_path=p, output_tokens=out_tokens
    )

    # Synthetic "actual": API client would report ~3.5 bytes/token on
    # English text. We re-compute the same arithmetic at the more
    # accurate ratio and call that the actual.
    actual_input_tokens = byte_len / 3.5
    actual_cost_float = (
        actual_input_tokens * 0.25 / 1_000_000
        + out_tokens * 1.25 / 1_000_000
    )
    actual_cost = Decimal(str(round(actual_cost_float, 6)))
    rel_error = abs(estimate - actual_cost) / actual_cost
    assert rel_error <= Decimal("0.30"), (
        f"estimator {estimate} differs from synthetic actual "
        f"{actual_cost} by {rel_error * 100}%; > 30% tolerance."
    )


# ---------------------------------------------------------------------------
# Determinism / pure-function contract.
# ---------------------------------------------------------------------------


def test_estimator_is_deterministic(tmp_path: Path) -> None:
    p = _write_constants(tmp_path / "c.json")
    a = estimate_extraction_cost(10_000, "claude-haiku-4-7", constants_path=p)
    b = estimate_extraction_cost(10_000, "claude-haiku-4-7", constants_path=p)
    assert a == b


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_unknown_model_rejected(tmp_path: Path) -> None:
    p = _write_constants(tmp_path / "c.json")
    with pytest.raises(CostConstantsError):
        estimate_extraction_cost(1000, "claude-ghost-1-0", constants_path=p)


def test_negative_byte_length_rejected(tmp_path: Path) -> None:
    p = _write_constants(tmp_path / "c.json")
    with pytest.raises(ValueError):
        estimate_extraction_cost(-1, "claude-haiku-4-7", constants_path=p)


def test_zero_byte_length_returns_output_only(tmp_path: Path) -> None:
    p = _write_constants(tmp_path / "c.json")
    cost = estimate_extraction_cost(
        0, "claude-haiku-4-7", constants_path=p, output_tokens=2000
    )
    # 0 input tokens + 2000 output * $1.25/M = $0.0025
    assert cost == Decimal("0.002500")


# ---------------------------------------------------------------------------
# Schema bound enforcement.
# ---------------------------------------------------------------------------


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CostConstantsError):
        load_cost_constants(tmp_path / "nope.json")


def test_invalid_json_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(CostConstantsError):
        load_cost_constants(p)


def test_negative_price_rejected(tmp_path: Path) -> None:
    p = _write_constants(tmp_path / "c.json", haiku_in=-0.01)
    with pytest.raises(CostConstantsError):
        load_cost_constants(p)


def test_non_usd_currency_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "artifact_type": "cost_constants",
                "schema_version": "1.0.0",
                "currency": "EUR",
                "constants": {
                    "claude-haiku-4-7": {
                        "input_per_million_tokens": 0.25,
                        "output_per_million_tokens": 1.25,
                    }
                },
                "last_updated": "2026-05-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CostConstantsError):
        load_cost_constants(p)


def test_default_output_tokens_per_family() -> None:
    assert DEFAULT_HAIKU_OUTPUT_TOKENS == 2_000
    assert DEFAULT_OPUS_OUTPUT_TOKENS == 10_000


def test_shipped_constants_validate() -> None:
    """The constants file committed in this PR validates."""
    doc = load_cost_constants()
    assert doc["schema_version"] == "1.0.0"
    assert "claude-opus-4-7" in doc["constants"]
    assert "claude-haiku-4-7" in doc["constants"]
