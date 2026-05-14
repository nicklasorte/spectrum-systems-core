"""Phase P3-A T-3: population rate tracker tests."""
from __future__ import annotations

from spectrum_systems_core.extraction.population_rates import (
    RATE_WARN_THRESHOLD,
    below_threshold_fields,
    compute_population_rates,
)


def test_all_fields_populated_yields_rates_of_one() -> None:
    decisions = [
        {
            "decision_text": "d1",
            "stakeholders": ["NTIA"],
            "rationale": "Because.",
        }
    ]
    claims = [{"claim_text": "c1", "claim_type": "regulatory"}]
    rates = compute_population_rates(decisions, claims)
    assert rates.stakeholders_populated_rate == 1.0
    assert rates.rationale_populated_rate == 1.0
    assert rates.claim_type_populated_rate == 1.0


def test_empty_lists_yield_zero_rate_not_divide_by_zero() -> None:
    rates = compute_population_rates([], [])
    assert rates.stakeholders_populated_rate == 0.0
    assert rates.rationale_populated_rate == 0.0
    assert rates.claim_type_populated_rate == 0.0


def test_empty_string_counts_as_not_populated() -> None:
    decisions = [
        {"decision_text": "d1", "stakeholders": [], "rationale": ""},
    ]
    rates = compute_population_rates(decisions, [])
    assert rates.stakeholders_populated_rate == 0.0
    assert rates.rationale_populated_rate == 0.0


def test_none_values_count_as_not_populated() -> None:
    decisions = [
        {"decision_text": "d1", "stakeholders": None, "rationale": None},
    ]
    rates = compute_population_rates(decisions, [])
    assert rates.stakeholders_populated_rate == 0.0
    assert rates.rationale_populated_rate == 0.0


def test_partial_population_produces_fractional_rate() -> None:
    decisions = [
        {"decision_text": "d1", "stakeholders": ["NTIA"], "rationale": ""},
        {"decision_text": "d2", "stakeholders": [], "rationale": "X"},
        {"decision_text": "d3", "stakeholders": ["FCC"], "rationale": "Y"},
        {"decision_text": "d4", "stakeholders": [], "rationale": None},
    ]
    rates = compute_population_rates(decisions, [])
    # 2 of 4 stakeholders populated; 2 of 4 rationale populated.
    assert rates.stakeholders_populated_rate == 0.5
    assert rates.rationale_populated_rate == 0.5


def test_below_threshold_fields_lists_only_under_threshold_fields() -> None:
    decisions = [
        {"decision_text": "d1", "stakeholders": ["NTIA"], "rationale": ""},
    ]
    claims = [{"claim_text": "c1", "claim_type": "regulatory"}]
    rates = compute_population_rates(decisions, claims)
    below = below_threshold_fields(rates, threshold=0.8)
    # stakeholders_populated_rate is 1.0 (above threshold) -- not listed
    # rationale_populated_rate is 0.0 (below threshold) -- listed
    # claim_type_populated_rate is 1.0 (above) -- not listed
    assert "rationale_populated_rate" in below
    assert "stakeholders_populated_rate" not in below
    assert "claim_type_populated_rate" not in below


def test_below_threshold_skips_zero_denominator_fields() -> None:
    # With zero decisions, the population rate for stakeholders /
    # rationale is technically 0.0 but the denominator is also 0,
    # so it would be misleading to flag it as below threshold.
    rates = compute_population_rates([], [{"claim_text": "c", "claim_type": "x"}])
    below = below_threshold_fields(rates)
    # Only claims fields should be eligible (and claim_type is 1.0).
    assert below == {}


def test_threshold_constant_is_zero_point_eight() -> None:
    assert RATE_WARN_THRESHOLD == 0.8
