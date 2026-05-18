"""Phase Y.4 — false-negative set builder.

A pure projection of ONE ``extraction_alignment_comparison`` artifact.
NO model calls, no I/O. The comparison artifact already carries the
fully-resolved ``false_negatives`` list (schema_type, ceiling_item_id,
source_turn_ids, source_text, ceiling_payload), so this step only has
to re-key it into its own artifact and re-sort it on the stable key.

Determinism: the payload is sorted by ``(schema_type,
ceiling_item_id)`` so two builds over the same comparison artifact
produce a byte-identical payload (``serialize.canonical_json``).
"""
from __future__ import annotations

from ..artifacts import Artifact, new_artifact

ARTIFACT_TYPE = "false_negative_set"
SCHEMA_VERSION = "1.0.0"


class FalseNegativeBuilderError(ValueError):
    """The input is not a usable extraction_alignment_comparison."""


def build_false_negative_set(comparison_artifact: Artifact) -> Artifact:
    if (
        comparison_artifact.artifact_type
        != "extraction_alignment_comparison"
    ):
        raise FalseNegativeBuilderError(
            "expected an extraction_alignment_comparison artifact, got "
            f"{comparison_artifact.artifact_type!r}"
        )
    payload = comparison_artifact.payload or {}
    raw = payload.get("false_negatives")
    if not isinstance(raw, list):
        raise FalseNegativeBuilderError(
            "comparison artifact has no false_negatives list"
        )
    false_negatives = sorted(
        (
            {
                "schema_type": str(fn.get("schema_type")),
                "ceiling_item_id": str(fn.get("ceiling_item_id")),
                "source_turn_ids": [
                    str(t) for t in (fn.get("source_turn_ids") or [])
                ],
                "source_text": str(fn.get("source_text") or ""),
                "ceiling_payload": fn.get("ceiling_payload")
                if isinstance(fn.get("ceiling_payload"), dict)
                else {},
            }
            for fn in raw
        ),
        key=lambda f: (f["schema_type"], f["ceiling_item_id"]),
    )
    out_payload = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": payload.get("transcript_id", ""),
        "comparison_artifact_id": comparison_artifact.artifact_id,
        "false_negatives": false_negatives,
    }
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=out_payload,
        trace_id=comparison_artifact.trace_id,
        status="draft",
        input_refs=[comparison_artifact.artifact_id],
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "FalseNegativeBuilderError",
    "build_false_negative_set",
]
