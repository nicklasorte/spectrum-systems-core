from __future__ import annotations

from typing import Iterable, Mapping, Optional

from ..artifacts import Artifact, ArtifactStore
from .gate import GroundingReport, verify_grounding


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


def grounding_gated_payload(
    artifact: Artifact,
    transcript: Optional[str],
    *,
    transcript_turn_ids: Optional[Iterable[str]] = None,
    min_quote_chars_by_type: Optional[Mapping[str, int]] = None,
) -> tuple[dict, GroundingReport]:
    """Phase 1 — run the grounding gate on ``artifact``'s payload.

    Returns ``(filtered_payload, report)`` where ``filtered_payload``
    is the original payload with each verifiable item-type array
    replaced by its accepted items (rejected items dropped). The
    envelope-level fields, the ``provenance`` block, and any payload
    field that is NOT in the gate's known item-type table pass
    through unchanged.

    The orchestrator is expected to:

    1. Build the meeting_minutes artifact (draft).
    2. Call ``grounding_gated_payload`` to get the filtered payload and
       the report.
    3. If ``report.artifact_blocked`` is True, the orchestrator emits
       a ``control_decision`` of ``block`` keyed by the gate's
       ``block_reason_code`` and the artifact is rejected via
       :func:`promote_if_allowed`. Otherwise, the orchestrator creates
       a new evaluated artifact carrying the filtered payload and
       promotes that.
    4. The orchestrator writes a ``grounding_rejection_report``
       diagnostic alongside the run's debug report, NEVER as a
       product artifact.

    Centralising the helper here closes the silent-pass path that
    Red-Team Pass 1 surfaced: callers no longer have to know how to
    invoke ``verify_grounding`` and reconstruct an accepted payload
    by hand.
    """
    payload = dict(artifact.payload)
    report = verify_grounding(
        payload,
        transcript,
        transcript_turn_ids=transcript_turn_ids,
        min_quote_chars_by_type=min_quote_chars_by_type,
    )
    accepted_by_type = report.accepted_payload_keys()
    # Replace each known item-type array with the accepted subset; an
    # absent type stays absent (so we don't add empty arrays the model
    # never emitted). Pass-through every other key.
    for item_type, accepted in accepted_by_type.items():
        payload[item_type] = list(accepted)
    # Item types that produced ONLY rejections must also be replaced
    # with an empty list so the rejected items do not survive in the
    # filtered payload.
    rejected_types = {r.item_type for r in report.rejected_items}
    for item_type in rejected_types:
        if item_type not in accepted_by_type:
            payload[item_type] = []
    return payload, report
