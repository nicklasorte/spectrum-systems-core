"""Phase AA.5 — auto-PR eligibility predicate for code candidates.

The single source of truth for "may this harness_code_candidate open a
PR automatically?". The GitHub workflow's script calls this; the AA.5
evaluator calls this to stamp ``auto_pr_eligible`` /
``eligibility_reason`` onto the very payload it writes, so the
artifact's verdict can never drift from the workflow's verdict (the
same invariant Phase Y.7 enforces for prompt candidates).

All FOUR conditions must hold:

* ``target_delta_f1            >= 0.05``
* ``holdout_delta_f1           >= 0.0``
* ``per_type_regressions       == []``
* ``allowlist_recheck_passed   is True``

Fail-closed: a missing or wrong-typed field is NOT a pass — it yields
``eligible=False`` / ``malformed_harness_code_candidate_evaluation`` so
a truncated artifact can never auto-open a PR.
"""
from __future__ import annotations

from dataclasses import dataclass

TARGET_DELTA_MIN = 0.05
HOLDOUT_DELTA_MIN = 0.0

ALL_CONDITIONS_MET = "all conditions met"


@dataclass(frozen=True)
class CodeEligibilityResult:
    eligible: bool
    reason: str  # "all conditions met" iff eligible, else "; "-joined


def evaluate_code_eligibility(evaluation: dict) -> CodeEligibilityResult:
    target_delta = evaluation.get("target_delta_f1")
    holdout_delta = evaluation.get("holdout_delta_f1")
    regressions = evaluation.get("per_type_regressions")
    recheck = evaluation.get("allowlist_recheck_passed")

    if (
        not isinstance(target_delta, (int, float))
        or not isinstance(holdout_delta, (int, float))
        or not isinstance(regressions, list)
        or not isinstance(recheck, bool)
    ):
        return CodeEligibilityResult(
            eligible=False,
            reason="malformed_harness_code_candidate_evaluation",
        )

    reasons: list[str] = []
    if float(target_delta) < TARGET_DELTA_MIN:
        reasons.append("target_delta_below_min")
    if float(holdout_delta) < HOLDOUT_DELTA_MIN:
        reasons.append("holdout_regression")
    if regressions:
        reasons.append("per_type_regressions_present")
    if recheck is not True:
        reasons.append("allowlist_recheck_failed")

    if reasons:
        return CodeEligibilityResult(
            eligible=False, reason="; ".join(reasons)
        )
    return CodeEligibilityResult(eligible=True, reason=ALL_CONDITIONS_MET)


__all__ = [
    "TARGET_DELTA_MIN",
    "HOLDOUT_DELTA_MIN",
    "ALL_CONDITIONS_MET",
    "CodeEligibilityResult",
    "evaluate_code_eligibility",
]
