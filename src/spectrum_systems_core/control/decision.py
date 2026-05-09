from __future__ import annotations

from ..artifacts import Artifact, new_artifact

ALLOWED_DECISIONS: frozenset[str] = frozenset({"allow", "warn", "freeze", "block"})


def decide_control(
    target_artifact: Artifact, eval_results: list[Artifact]
) -> Artifact:
    reason_codes: list[str] = []

    if not eval_results:
        decision = "block"
        reason_codes.append("missing_required_evals")
    else:
        failed = [
            r for r in eval_results if r.payload.get("status") == "fail"
        ]
        if failed:
            decision = "block"
            for r in failed:
                reason_codes.append(
                    f"failed:{r.payload.get('eval_type', 'unknown')}"
                )
        else:
            decision = "allow"

    payload = {
        "target_artifact_id": target_artifact.artifact_id,
        "decision": decision,
        "reason_codes": reason_codes,
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
