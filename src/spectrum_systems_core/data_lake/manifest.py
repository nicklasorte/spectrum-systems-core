"""Minimal replay manifest for one Produce -> Evaluate -> Decide -> Promote run.

A manifest is the single record that lets a new engineer recreate or
explain a run without scraping logs. It is deliberately small — no proof
system, no signature chain, just enough to verify "same inputs -> same
outputs".

Required fields: see REQUIRED_MANIFEST_FIELDS below.
"""
from __future__ import annotations

from typing import Any

from ..artifacts import compute_content_hash
from .serialize import canonical_json

MANIFEST_SCHEMA_VERSION = 1

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "trace_id",
    "meeting_id",
    "workflow_name",
    "input_transcript_hash",
    "input_metadata_hash",
    "produced_artifacts",
    "eval_artifacts",
    "control_decision",
    "promoted_artifact_ids",
)


class ManifestError(ValueError):
    """Raised when a manifest violates the contract."""


def _artifact_ref(artifact) -> dict[str, str]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "content_hash": artifact.content_hash,
    }


def derive_run_id(*, trace_id: str, workflow_name: str, meeting_id: str) -> str:
    """run_id is deterministic for the same trace/workflow/meeting tuple."""
    seed = compute_content_hash(
        {"trace_id": trace_id, "workflow_name": workflow_name, "meeting_id": meeting_id}
    )
    return f"run-{seed[:16]}"


def build_manifest(
    *,
    transcript_input,
    workflow_name: str,
    produced_artifacts: list,
    eval_artifacts: list,
    control_decision,
    promoted_artifact_ids: list[str],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a manifest dict. The dict is canonical-JSON ready."""
    if not produced_artifacts:
        raise ManifestError("manifest requires at least one produced artifact")
    if control_decision is None:
        raise ManifestError("manifest requires a control_decision artifact")

    trace_id = produced_artifacts[0].trace_id
    actual_run_id = run_id or derive_run_id(
        trace_id=trace_id,
        workflow_name=workflow_name,
        meeting_id=transcript_input.meeting_id,
    )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": actual_run_id,
        "trace_id": trace_id,
        "meeting_id": transcript_input.meeting_id,
        "workflow_name": workflow_name,
        "input_transcript_hash": transcript_input.transcript_hash,
        "input_metadata_hash": transcript_input.metadata_hash,
        "produced_artifacts": sorted(
            (_artifact_ref(a) for a in produced_artifacts),
            key=lambda d: (d["artifact_type"], d["artifact_id"]),
        ),
        "eval_artifacts": sorted(
            (_artifact_ref(a) for a in eval_artifacts),
            key=lambda d: (d["artifact_type"], d["artifact_id"]),
        ),
        "control_decision": {
            "artifact_id": control_decision.artifact_id,
            "decision": control_decision.payload.get("decision"),
            "reason_codes": list(control_decision.payload.get("reason_codes", [])),
            "content_hash": control_decision.content_hash,
        },
        "promoted_artifact_ids": sorted(promoted_artifact_ids),
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            raise ManifestError(f"manifest missing required field: {field}")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"manifest schema_version mismatch: "
            f"got {manifest['schema_version']!r}, "
            f"expected {MANIFEST_SCHEMA_VERSION}"
        )
    if not isinstance(manifest["produced_artifacts"], list) or not manifest["produced_artifacts"]:
        raise ManifestError("manifest.produced_artifacts must be a non-empty list")
    if not isinstance(manifest["promoted_artifact_ids"], list):
        raise ManifestError("manifest.promoted_artifact_ids must be a list")


def manifest_to_json(manifest: dict[str, Any]) -> str:
    validate_manifest(manifest)
    return canonical_json(manifest)
