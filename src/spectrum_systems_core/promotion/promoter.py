from __future__ import annotations

from ..artifacts import Artifact, ArtifactStore


def promote_if_allowed(
    store: ArtifactStore,
    target_artifact: Artifact,
    control_decision: Artifact,
) -> Artifact:
    if control_decision.artifact_type != "control_decision":
        raise ValueError(
            "promote_if_allowed requires a control_decision artifact"
        )
    if control_decision.payload.get("target_artifact_id") != target_artifact.artifact_id:
        raise ValueError(
            "control_decision does not target the supplied artifact"
        )

    decision = control_decision.payload.get("decision")
    new_status = "promoted" if decision == "allow" else "rejected"
    return store.update_status(target_artifact.artifact_id, new_status)
