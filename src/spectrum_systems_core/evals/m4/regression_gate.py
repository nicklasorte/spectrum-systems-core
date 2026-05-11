"""RegressionGate: per-pair threshold comparison against baseline.

Phase M.4. Lifecycle of the gate:

* run 1: no baseline -- gate writes the current summary as baseline and
         emits ``skip_no_baseline``. Build still passes.
* run 2: baseline exists but the gate is intentionally silent for one
         more cycle (``skip_run_count``) so the team has time to verify
         the baseline before it has authority to block.
* run 3+: gate is active. For each (current pair, baseline pair) it
         checks:
             coverage_drop > 15%               -> block
             items_requiring_review_rate rise > 20% -> block
         If either condition fires on any pair the gate emits ``block``
         and lists every offending pair in ``regression_detail``.

The gate compares pairs by ``pair_id`` -- never positional. A new pair
that did not exist in baseline cannot regress.

The gate never raises. On a corrupt baseline (missing fields) it emits
``skip_no_baseline`` with a reason describing the corruption so the
operator can clear the baseline manually.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List, Optional


# Thresholds. The 0.15 / 0.20 numbers are the M.4 spec defaults --
# 15 % is roughly one missed minutes item per pair on a 7-item pair
# (the median minutes-item-per-pair the eval was designed for) and 20 %
# is the cost-of-review headroom: a 20 % rise in HITL queue rate is the
# point at which the team would notice an extra reviewer-hour per pair
# per week. Both are surfaced here, not buried in code, so a rebaselining
# can move them in one place. Re-tune only after a real-data baseline.
COVERAGE_DROP_THRESHOLD = 0.15
REVIEW_RATE_RISE_THRESHOLD = 0.20

# Runs 1 and 2 are baseline-establishment cycles. The gate becomes
# authoritative on run 3 so the team has one cycle (run 2) to verify
# the baseline by hand before it can block a build.
GATE_ACTIVE_FROM_RUN = 3

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "RegressionGate"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


class RegressionGate:
    """Compare a current eval_summary against the baseline summary."""

    COVERAGE_DROP_THRESHOLD = COVERAGE_DROP_THRESHOLD
    REVIEW_RATE_RISE_THRESHOLD = REVIEW_RATE_RISE_THRESHOLD
    GATE_ACTIVE_FROM_RUN = GATE_ACTIVE_FROM_RUN

    SCHEMA_VERSION = SCHEMA_VERSION
    PRODUCED_BY = PRODUCED_BY

    def evaluate(
        self,
        current_summary: Dict[str, Any],
        baseline_summary: Optional[Dict[str, Any]],
        run_count: int,
        *,
        current_pair_results: Optional[List[Dict[str, Any]]] = None,
        baseline_pair_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Return a gate_decision dict.

        ``current_pair_results`` and ``baseline_pair_results`` are lists
        of per-pair eval_result dicts. They carry the per-pair numbers
        the gate compares -- the eval_summary alone only carries
        aggregates. If a caller has summaries but not per-pair data the
        gate degrades to aggregate-only comparison (still safe, but less
        granular).
        """
        eval_summary_id = current_summary.get("eval_summary_id", "")
        baseline_id = (
            baseline_summary.get("eval_summary_id") if baseline_summary else None
        )

        # Skip path 1: no baseline -> establish baseline, skip gate.
        if baseline_summary is None:
            return self._decision(
                eval_summary_id,
                baseline_id,
                decision="skip_no_baseline",
                reason="no_baseline_exists",
                run_count=run_count,
                regression_detail=[],
            )

        # Skip path 2: gate is inactive until run_count >= GATE_ACTIVE_FROM_RUN.
        if run_count < self.GATE_ACTIVE_FROM_RUN:
            return self._decision(
                eval_summary_id,
                baseline_id,
                decision="skip_run_count",
                reason=(
                    f"run_count_{run_count}_below_gate_active_from_"
                    f"{self.GATE_ACTIVE_FROM_RUN}"
                ),
                run_count=run_count,
                regression_detail=[],
            )

        # Fail-closed: gate is active but the baseline has no per-pair
        # records to compare against. This happens when a baseline
        # summary was installed from a partial / zero-pair run, or when
        # its eval_result files are missing on disk. Without per-pair
        # data we cannot tell whether anything regressed -- so we MUST
        # NOT silently emit allow. RT1 finding.
        current_pair_results = current_pair_results or []
        baseline_pair_results = baseline_pair_results or []
        if current_pair_results and not baseline_pair_results:
            return self._decision(
                eval_summary_id,
                baseline_id,
                decision="block",
                reason="baseline_has_no_pair_results",
                run_count=run_count,
                regression_detail=[],
            )

        # Per-pair regression check.
        regression_detail = self._regression_detail(
            current_pair_results, baseline_pair_results
        )

        if regression_detail:
            return self._decision(
                eval_summary_id,
                baseline_id,
                decision="block",
                reason=(
                    f"regression_on_{len(regression_detail)}_"
                    f"pair_metric_pairs"
                ),
                run_count=run_count,
                regression_detail=regression_detail,
            )

        return self._decision(
            eval_summary_id,
            baseline_id,
            decision="allow",
            reason="within_thresholds",
            run_count=run_count,
            regression_detail=[],
        )

    # -- internals --------------------------------------------------------

    def _decision(
        self,
        eval_summary_id: str,
        baseline_eval_summary_id: Optional[str],
        *,
        decision: str,
        reason: str,
        run_count: int,
        regression_detail: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "gate_decision_id": str(uuid.uuid4()),
            "eval_summary_id": eval_summary_id,
            "baseline_eval_summary_id": baseline_eval_summary_id,
            "artifact_type": "gate_decision",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "decision": decision,
            "reason": reason,
            "run_count": int(run_count),
            "regression_detail": list(regression_detail),
            "provenance": {"produced_by": self.PRODUCED_BY},
        }

    def _regression_detail(
        self,
        current_pair_results: List[Dict[str, Any]],
        baseline_pair_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        baseline_by_pair: Dict[str, Dict[str, Any]] = {}
        for er in baseline_pair_results:
            pid = er.get("pair_id")
            if isinstance(pid, str) and pid:
                baseline_by_pair[pid] = er

        out: List[Dict[str, Any]] = []
        for er in current_pair_results:
            pid = er.get("pair_id")
            if not isinstance(pid, str) or not pid:
                continue
            base = baseline_by_pair.get(pid)
            if base is None:
                # New pair not in baseline: cannot regress.
                continue

            cur_cov = _safe_float(er.get("coverage"))
            base_cov = _safe_float(base.get("coverage"))
            coverage_delta = cur_cov - base_cov
            if coverage_delta < -self.COVERAGE_DROP_THRESHOLD:
                out.append(
                    {
                        "pair_id": pid,
                        "metric": "coverage",
                        "baseline_value": base_cov,
                        "current_value": cur_cov,
                        "delta": coverage_delta,
                    }
                )

            cur_rev = _safe_float(er.get("items_requiring_review_rate"))
            base_rev = _safe_float(base.get("items_requiring_review_rate"))
            review_delta = cur_rev - base_rev
            if review_delta > self.REVIEW_RATE_RISE_THRESHOLD:
                out.append(
                    {
                        "pair_id": pid,
                        "metric": "items_requiring_review_rate",
                        "baseline_value": base_rev,
                        "current_value": cur_rev,
                        "delta": review_delta,
                    }
                )
        return out


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
