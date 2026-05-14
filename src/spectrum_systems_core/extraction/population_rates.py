"""Phase P3-A T-3: extended-field population rate tracking.

The decision schema gained ``stakeholders`` and ``rationale`` in
Phase T; the claim schema gained ``claim_type`` even earlier. These
fields are NOT schema-required because we want backward-compatible
artifacts. The trade-off is that a prompt regression can silently
stop populating them and no schema validator catches it.

This module computes the per-field population RATE so a regression
is visible in the eval_summary. A rate < ``RATE_WARN_THRESHOLD``
(default 0.8) emits a ``low_field_population_rate`` finding (warn,
not halt) so the gate is fail-OPEN by design: the goal is to
surface prompt-tuning needs, not block the run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Sequence


# Rates strictly below this threshold emit a warn finding. 0.8 is
# a pragmatic threshold: most prompts that produce an empty
# population rate on a meaningful field exhibit a regression.
RATE_WARN_THRESHOLD: float = 0.8


@dataclass(frozen=True)
class PopulationRates:
    stakeholders_populated_rate: float
    rationale_populated_rate: float
    claim_type_populated_rate: float
    decisions_total: int
    claims_total: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stakeholders_populated_rate": self.stakeholders_populated_rate,
            "rationale_populated_rate": self.rationale_populated_rate,
            "claim_type_populated_rate": self.claim_type_populated_rate,
            "decisions_total": self.decisions_total,
            "claims_total": self.claims_total,
        }


def _value_is_populated(value: Any) -> bool:
    """Population test: a non-empty string OR a non-empty list.

    None, an empty string, and an empty list ALL count as "not
    populated". The model returning ``[]`` for stakeholders or
    ``""`` for rationale is the regression we want to catch.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return len(value) > 0
    # Any other type: treat as populated. The schema would reject
    # an unexpected type at write time; this function is a
    # rate-tracker, not a type-checker.
    return True


def compute_population_rates(
    decisions: Sequence[Dict[str, Any]],
    claims: Sequence[Dict[str, Any]],
) -> PopulationRates:
    """Compute per-field population rates across the extracted items.

    Rates are floats in [0, 1]. Denominator-zero collapses to 0.0
    so a transcript with zero decisions cannot DivideByZero or
    silently report a "100% populated" rate.
    """
    decisions = list(decisions or [])
    claims = list(claims or [])

    decisions_total = len(decisions)
    claims_total = len(claims)

    stakeholders_populated = sum(
        1 for d in decisions
        if isinstance(d, dict) and _value_is_populated(d.get("stakeholders"))
    )
    rationale_populated = sum(
        1 for d in decisions
        if isinstance(d, dict) and _value_is_populated(d.get("rationale"))
    )
    claim_type_populated = sum(
        1 for c in claims
        if isinstance(c, dict) and _value_is_populated(c.get("claim_type"))
    )

    def _rate(num: int, denom: int) -> float:
        return num / denom if denom > 0 else 0.0

    return PopulationRates(
        stakeholders_populated_rate=_rate(stakeholders_populated, decisions_total),
        rationale_populated_rate=_rate(rationale_populated, decisions_total),
        claim_type_populated_rate=_rate(claim_type_populated, claims_total),
        decisions_total=decisions_total,
        claims_total=claims_total,
    )


def below_threshold_fields(
    rates: PopulationRates,
    *,
    threshold: float = RATE_WARN_THRESHOLD,
) -> Dict[str, float]:
    """Return the rates strictly below ``threshold`` (only the
    fields that have a denominator > 0).

    Used by the runner to decide whether to emit a
    ``low_field_population_rate`` finding. The decision is fail-OPEN:
    a finding is surfaced for diagnosis but never halts the run.
    """
    out: Dict[str, float] = {}
    if rates.decisions_total > 0:
        if rates.stakeholders_populated_rate < threshold:
            out["stakeholders_populated_rate"] = rates.stakeholders_populated_rate
        if rates.rationale_populated_rate < threshold:
            out["rationale_populated_rate"] = rates.rationale_populated_rate
    if rates.claims_total > 0:
        if rates.claim_type_populated_rate < threshold:
            out["claim_type_populated_rate"] = rates.claim_type_populated_rate
    return out


__all__ = [
    "PopulationRates",
    "RATE_WARN_THRESHOLD",
    "below_threshold_fields",
    "compute_population_rates",
]
