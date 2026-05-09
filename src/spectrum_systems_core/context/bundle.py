from __future__ import annotations

from ..artifacts import Artifact, new_artifact


def build_context_bundle(input_text: str, purpose: str, *, trace_id: str = "") -> Artifact:
    payload = {
        "purpose": purpose,
        "source_text": input_text,
        "assumptions": [],
        "source_refs": [],
    }
    return new_artifact(
        artifact_type="context_bundle",
        payload=payload,
        trace_id=trace_id,
        status="draft",
    )
