"""VerificationGate: Phase V Gate-2.

Two-stage check on a meeting_extraction + source_verification_result
pair. Stage 1 confirms every extracted item has a verification entry.
Stage 2 confirms every entry's status is ``verified``. Either failure
returns ``passed=False`` with a structured reason so callers can route
to HITL.

The gate is short-circuited when the Phase V feature flag is disabled
so build-time tooling that always invokes the gate gets a stable
``passed=True, reason="phase_v_disabled"`` decision.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from ..config.feature_flag import PHASE_V_FLAG_NAME, FeatureFlag

_NON_VERIFIED_STATUSES = (
    "unsupported",
    "contradicted",
    "insufficient_evidence",
    "verification_failed",
)


@dataclass
class GateDecision:
    """Outcome of one gate evaluation."""

    passed: bool
    reason: str
    stage: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "reason": self.reason,
            "stage": self.stage,
            "details": dict(self.details),
        }


class VerificationGate:
    """Two-stage Phase V gate."""

    FLAG_NAME = PHASE_V_FLAG_NAME

    def check_phase_v_verification(
        self,
        meeting_extraction: dict[str, Any],
        verification_result: dict[str, Any] | None,
        data_lake_path: str | pathlib.Path,
    ) -> GateDecision:
        if not FeatureFlag(data_lake_path).is_enabled(self.FLAG_NAME):
            return GateDecision(
                passed=True,
                reason="phase_v_disabled",
                stage="phase_v_skipped",
            )

        # Flag is enabled but no verification artifact was produced --
        # the pipeline did not run the verifier. Fail-closed.
        if not isinstance(verification_result, dict):
            return GateDecision(
                passed=False,
                reason="verification_result_missing",
                stage="phase_v_stage_1_completeness",
                details={"verification_result": None},
            )

        # Mid-flight halt always blocks.
        summary = verification_result.get("summary") or {}
        if summary.get("status") == "halted_sanity_check":
            return GateDecision(
                passed=False,
                reason="verification_halted_sanity_check",
                stage="phase_v_stage_1_completeness",
                details={
                    "verified_count": summary.get("verified_count", 0),
                    "unsupported_count": summary.get("unsupported_count", 0),
                    "total_items_count": summary.get("total_items_count", 0),
                },
            )

        # Stage 1: every extracted item must have a verification entry.
        all_item_ids = _collect_item_ids(meeting_extraction)
        verified_item_ids = {
            v.get("item_id")
            for v in (verification_result.get("item_verifications") or [])
            if isinstance(v, dict) and isinstance(v.get("item_id"), str)
        }
        missing = all_item_ids - verified_item_ids
        if missing:
            return GateDecision(
                passed=False,
                reason="verification_incomplete",
                stage="phase_v_stage_1_completeness",
                details={"missing_item_ids": sorted(missing)},
            )

        # Stage 2: every verification entry must be ``verified``.
        non_verified: list[dict[str, Any]] = [
            v for v in (verification_result.get("item_verifications") or [])
            if isinstance(v, dict)
            and v.get("verification_status") != "verified"
        ]

        if non_verified:
            breakdown = {
                status: sum(
                    1 for v in non_verified
                    if v.get("verification_status") == status
                )
                for status in _NON_VERIFIED_STATUSES
            }
            # Anything outside the enum lands in 'unknown' so triage
            # is not misled by a 0-everywhere breakdown (RT1 Sev-2 fix).
            known = set(_NON_VERIFIED_STATUSES) | {"verified"}
            breakdown["unknown"] = sum(
                1 for v in non_verified
                if v.get("verification_status") not in known
            )
            return GateDecision(
                passed=False,
                reason="items_failed_verification",
                stage="phase_v_stage_2_status",
                details={
                    "failed_item_count": len(non_verified),
                    "failed_statuses_breakdown": breakdown,
                    "failed_item_ids": [v.get("item_id") for v in non_verified],
                },
            )

        return GateDecision(passed=True, reason="all_items_verified")


def _collect_item_ids(meeting_extraction: dict[str, Any]) -> set:
    from .post_hoc_verifier import _coerce_item_id
    ids: set = set()
    for key in ("decisions", "claims", "action_items"):
        for item in meeting_extraction.get(key, []) or []:
            if isinstance(item, dict):
                ids.add(_coerce_item_id(item))
    return ids
