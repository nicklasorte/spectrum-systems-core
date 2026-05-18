"""Phase Y.7 — auto-PR eligibility predicate.

The single source of truth for "may this candidate open a PR
automatically?". The GitHub workflow calls this; the Y.6 evaluator
calls this to stamp ``auto_pr_eligible`` / ``eligibility_reason`` onto
the artifact. Keeping the logic in ONE module is why the workflow YAML
contains no eligibility logic of its own (Phase Y brief: "extract it
into a Python module the workflow calls — do not embed logic in YAML")
and why the artifact's stamped verdict can never drift from the
workflow's verdict.

All three conditions must hold:

* ``target_delta_f1   >= 0.05``
* ``holdout_delta_f1  >= 0.0``
* ``per_type_regressions == []``

Fail-closed: a missing or wrong-typed field is NOT treated as a pass —
it yields ``eligible=False`` with ``malformed_candidate_evaluation`` so
a truncated artifact can never auto-open a PR.
"""
from __future__ import annotations

from dataclasses import dataclass

TARGET_DELTA_MIN = 0.05
HOLDOUT_DELTA_MIN = 0.0


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: list[str]  # empty iff eligible


def evaluate_eligibility(candidate_evaluation: dict) -> EligibilityResult:
    reasons: list[str] = []
    target_delta = candidate_evaluation.get("target_delta_f1")
    holdout_delta = candidate_evaluation.get("holdout_delta_f1")
    regressions = candidate_evaluation.get("per_type_regressions")

    if not isinstance(target_delta, (int, float)) or not isinstance(
        holdout_delta, (int, float)
    ) or not isinstance(regressions, list):
        return EligibilityResult(
            eligible=False,
            reasons=["malformed_candidate_evaluation"],
        )

    if float(target_delta) < TARGET_DELTA_MIN:
        reasons.append("target_delta_below_min")
    if float(holdout_delta) < HOLDOUT_DELTA_MIN:
        reasons.append("holdout_regression")
    if regressions:
        reasons.append("per_type_regressions_present")

    return EligibilityResult(eligible=not reasons, reasons=reasons)


__all__ = [
    "TARGET_DELTA_MIN",
    "HOLDOUT_DELTA_MIN",
    "EligibilityResult",
    "evaluate_eligibility",
]
