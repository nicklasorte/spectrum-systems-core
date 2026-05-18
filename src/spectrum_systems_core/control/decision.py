from __future__ import annotations

import os

from ..artifacts import Artifact, new_artifact

ALLOWED_DECISIONS: frozenset[str] = frozenset({"allow", "warn", "freeze", "block"})

# Phase Y.3 thresholds (frozen — a change here is a gate change and
# needs its own rejection test).
_TOTAL_F1_THRESHOLD = 0.70
_PER_TYPE_F1_FLOOR = 0.50
_PER_TYPE_COUNT_FLOOR = 3

COMPARISON_GATE_ENV = "COMPARISON_GATE_ENABLED"


def _comparison_gate_enabled(env: dict | None = None) -> bool:
    """Default ``True``. Set ``COMPARISON_GATE_ENABLED`` to a falsey
    token (0/false/no/off, case-insensitive) to suppress ONLY the new
    Phase Y.3 block reasons. The comparison artifact is still produced
    and every pre-existing fail-closed rule is untouched — this is the
    Y.3 rollback switch, not a global kill switch.
    """
    environ = env if env is not None else os.environ
    raw = (environ.get(COMPARISON_GATE_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _comparison_block_reasons(target_artifact: Artifact) -> list[str]:
    """Y.3 block reasons for an ``extraction_alignment_comparison``
    target. Fail-closed: a missing/!dict ``total_metrics`` (or a
    missing ``f1``) blocks rather than passes by absence (red-team
    Pass 1 #2 — a gate must not be bypassable by withholding the field
    it reads)."""
    reasons: list[str] = []
    payload = target_artifact.payload or {}

    # Red-team Pass 1 #1 (defence in depth): the comparator already
    # refuses to PRODUCE a comparison whose alignment_contract_version
    # drifts from the binding file. Re-check here so an artifact that
    # reached control any other way (hand-forged, stale on disk) cannot
    # pass the F1 gate under a drifted predicate. Lazy import keeps
    # sklearn off the common control import path.
    declared_version = payload.get("alignment_contract_version")
    try:
        from ..evals.extraction_comparison import contract_version

        file_version = contract_version()
    except Exception:  # noqa: BLE001 — unreadable contract = fail closed
        reasons.append("comparison_contract_version_unverifiable")
    else:
        if declared_version != file_version:
            reasons.append("comparison_contract_version_mismatch")

    total = payload.get("total_metrics")
    if not isinstance(total, dict) or not isinstance(
        total.get("f1"), (int, float)
    ):
        reasons.append("comparison_total_metrics_missing")
    elif float(total["f1"]) < _TOTAL_F1_THRESHOLD:
        reasons.append("comparison_total_f1_below_threshold")

    per_type = payload.get("per_type_metrics")
    if not isinstance(per_type, dict):
        reasons.append("comparison_per_type_metrics_missing")
    else:
        for stype in sorted(per_type):
            metrics = per_type[stype]
            if not isinstance(metrics, dict):
                reasons.append(f"comparison_per_type_metrics_malformed:{stype}")
                continue
            f1 = metrics.get("f1")
            ceiling_count = metrics.get("ceiling_count")
            if not isinstance(f1, (int, float)) or not isinstance(
                ceiling_count, int
            ):
                reasons.append(
                    f"comparison_per_type_metrics_malformed:{stype}"
                )
                continue
            if float(f1) < _PER_TYPE_F1_FLOOR and (
                ceiling_count >= _PER_TYPE_COUNT_FLOOR
            ):
                reasons.append(
                    f"comparison_per_type_f1_below_floor:{stype}"
                )
    return reasons


def decide_control(
    target_artifact: Artifact, eval_results: list[Artifact]
) -> Artifact:
    reason_codes: list[str] = []
    # within_source warn codes from any eval_result the router demoted
    # (status == "warn"). These are recorded on the control_decision
    # (a fresh envelope — no in-place payload edit) so the decision
    # itself carries the warn provenance, and the workflow can copy
    # them onto the promoted artifact's provenance and into
    # eval_history.jsonl for the correction miner.
    within_source_warnings: list[str] = []

    if not eval_results:
        decision = "block"
        reason_codes.append("missing_required_evals")
    else:
        # Blocking is ``status == "fail"`` ONLY. A demoted within_source
        # miss carries ``status == "warn"`` — it is logged, never
        # blocking. ``pass`` is neither. No pre-existing eval emits
        # "warn", so every pre-existing gate is byte-identical: a real
        # fail still blocks exactly as before.
        failed = [
            r for r in eval_results if r.payload.get("status") == "fail"
        ]
        warned = [
            r for r in eval_results if r.payload.get("status") == "warn"
        ]
        for r in warned:
            for rc in r.payload.get("reason_codes") or []:
                if isinstance(rc, str):
                    within_source_warnings.append(rc)
        if failed:
            decision = "block"
            for r in failed:
                reason_codes.append(
                    f"failed:{r.payload.get('eval_type', 'unknown')}"
                )
        else:
            decision = "allow"

    # Phase Y.3 — additive comparison gate. It can only ever turn an
    # allow into a block (or add reasons to an already-blocked run); it
    # never relaxes the pre-existing fail-closed logic above.
    if (
        target_artifact.artifact_type == "extraction_alignment_comparison"
        and _comparison_gate_enabled()
    ):
        comparison_reasons = _comparison_block_reasons(target_artifact)
        if comparison_reasons:
            decision = "block"
            reason_codes.extend(comparison_reasons)

    payload = {
        "target_artifact_id": target_artifact.artifact_id,
        "decision": decision,
        "reason_codes": reason_codes,
        # Sorted + de-duplicated for determinism (the data-lake
        # contract requires byte-identical outputs given the same
        # inputs). Empty list on the common no-warn path so the field
        # is always present and shape-stable.
        "within_source_warnings": sorted(set(within_source_warnings)),
        "eval_result_refs": [r.artifact_id for r in eval_results],
    }
    return new_artifact(
        artifact_type="control_decision",
        payload=payload,
        trace_id=target_artifact.trace_id,
        status="evaluated",
        input_refs=[target_artifact.artifact_id]
        + [r.artifact_id for r in eval_results],
    )
