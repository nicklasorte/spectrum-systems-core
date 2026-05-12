"""Phase X2.3 — calibration of the judge against ground-truth pairs.

The judge's verdict is only useful insofar as it AGREES with the human
ground truth on a meaningful slice of items. This module computes:

  * ``agreement_rate_overall`` — fraction of (ground_truth, judge)
    pairs that agree on pass/fail, across all matched pairs.
  * ``agreement_rate_verb_discrimination`` — same fraction restricted
    to GT pairs flagged with
    ``rubric_notes.verb_discrimination_example == true``. This is the
    metric that directly validates Rec 9 (regulatory verb
    discrimination is not interchangeable).

Severity bands:
  * agreement >= 0.70 -> ``ok``
  * 0.60 <= agreement < 0.70 -> ``warn`` (judge_calibration_low)
  * agreement < 0.60 -> ``halt`` (judge_calibration_failed)

When zero verb-discrimination pairs are present,
``agreement_rate_verb_discrimination`` is ``None`` and no warn is
raised — the judge simply has nothing to be calibrated against on
that dimension yet.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..health.finding import HealthFinding
from .judge import (
    JUDGE_CALIBRATION_HALT_THRESHOLD,
    JUDGE_CALIBRATION_WARN_THRESHOLD,
    JudgeRunResult,
)


CALIBRATION_SCHEMA_VERSION: str = "1.0.0"
PRODUCED_BY: str = "JudgeCalibrationRunner"


@dataclass
class JudgeCalibrationRecord:
    record_id: str
    judge_score_id: str
    pairs_compared: int
    agreement_rate_overall: Optional[float]
    agreement_rate_verb_discrimination: Optional[float]
    verb_discrimination_pairs: int
    calibration_status: str  # ok | warn | failed
    findings: List[HealthFinding] = field(default_factory=list)


def _agreement(matches: List[bool]) -> Optional[float]:
    if not matches:
        return None
    return sum(1 for m in matches if m) / float(len(matches))


def _judge_pass_for_item(
    judge_result: JudgeRunResult, item_id: str
) -> Optional[bool]:
    for s in judge_result.item_scores:
        if s.item_id == item_id:
            if s.judge_decision == "unparseable":
                return None
            return s.passed
    return None


def calibrate(
    judge_result: JudgeRunResult,
    ground_truth_pairs: Sequence[Dict[str, Any]],
    *,
    pipeline_run_id: Optional[str] = None,
) -> JudgeCalibrationRecord:
    """Compute agreement between the judge and the ground truth.

    Each GT pair must carry:
      * ``pair_id``
      * ``target_type == "decision"`` (others are ignored)
      * a ``decision_id`` field linking back to the judged item
      * optionally, ``ground_truth_pass`` (bool) — the human verdict
      * optionally, ``rubric_notes.verb_discrimination_example``

    GT pairs whose ``decision_id`` does not appear in the judge result
    are silently skipped (the judge may have judged a subset of the
    items the GT covers).
    """
    overall_matches: List[bool] = []
    verb_matches: List[bool] = []
    verb_pairs_count = 0

    for pair in ground_truth_pairs:
        if not isinstance(pair, dict):
            continue
        if pair.get("target_type") != "decision":
            continue
        decision_id = pair.get("decision_id") or pair.get("item_id")
        gt_pass = pair.get("ground_truth_pass")
        if not isinstance(decision_id, str) or not isinstance(gt_pass, bool):
            continue
        judge_pass = _judge_pass_for_item(judge_result, decision_id)
        if judge_pass is None:
            continue
        match = (gt_pass == judge_pass)
        overall_matches.append(match)
        rubric_notes = pair.get("rubric_notes")
        if isinstance(rubric_notes, dict) and bool(
            rubric_notes.get("verb_discrimination_example")
        ):
            verb_pairs_count += 1
            verb_matches.append(match)

    overall = _agreement(overall_matches)
    verb = _agreement(verb_matches)

    findings: List[HealthFinding] = []
    status = "ok"
    if overall is None:
        # No comparable pairs at all: the calibration step is "unrun"
        # for this run; surface nothing.
        status = "unrun"
    elif overall < JUDGE_CALIBRATION_HALT_THRESHOLD:
        status = "failed"
        findings.append(HealthFinding(
            finding_code="judge_calibration_failed",
            severity="halt",
            pipeline_run_id=pipeline_run_id,
            context={
                "agreement_rate_overall": overall,
                "halt_threshold": JUDGE_CALIBRATION_HALT_THRESHOLD,
                "pairs_compared": len(overall_matches),
            },
            remediation=(
                "Investigate the judge prompt and rubric. Do not run "
                "--set-baseline while calibration is failed."
            ),
        ))
    elif overall < JUDGE_CALIBRATION_WARN_THRESHOLD:
        status = "warn"
        findings.append(HealthFinding(
            finding_code="judge_calibration_low",
            severity="warn",
            pipeline_run_id=pipeline_run_id,
            context={
                "agreement_rate_overall": overall,
                "warn_threshold": JUDGE_CALIBRATION_WARN_THRESHOLD,
                "pairs_compared": len(overall_matches),
            },
            remediation=(
                "Inspect mismatched pairs in the judge_score artifact "
                "before treating judge_aggregate_pass_rate as a quality "
                "signal."
            ),
        ))

    return JudgeCalibrationRecord(
        record_id=str(uuid.uuid4()),
        judge_score_id=judge_result.judge_score_id,
        pairs_compared=len(overall_matches),
        agreement_rate_overall=overall,
        agreement_rate_verb_discrimination=verb,
        verb_discrimination_pairs=verb_pairs_count,
        calibration_status=status,
        findings=findings,
    )


def calibration_to_artifact(
    record: JudgeCalibrationRecord,
) -> Dict[str, Any]:
    return {
        "artifact_type": "judge_calibration_record",
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "record_id": record.record_id,
        "judge_score_id": record.judge_score_id,
        "pairs_compared": record.pairs_compared,
        "agreement_rate_overall": record.agreement_rate_overall,
        "agreement_rate_verb_discrimination": (
            record.agreement_rate_verb_discrimination
        ),
        "verb_discrimination_pairs": record.verb_discrimination_pairs,
        "calibration_status": record.calibration_status,
        "provenance": {"produced_by": PRODUCED_BY},
    }


__all__ = [
    "CALIBRATION_SCHEMA_VERSION",
    "JudgeCalibrationRecord",
    "calibrate",
    "calibration_to_artifact",
]
