"""Transcript pipeline orchestrator.

Loads raw inputs, runs the governed loop with grounding, writes promoted
artifacts, builds the manifest and debug report, and returns a small
result object that tests and the index/query layers can read.

This is the only module that ties data lake I/O to the core loop. Keeping
it in one file keeps the boundary easy to audit.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts import Artifact, ArtifactStore, compute_content_hash, new_artifact
from ..context import build_context_bundle
from ..control import decide_control
from ..evals import run_required_evals
from ..promotion import promote_if_allowed
from .debug import build_debug_report
from .extract import build_grounded_payload, supported_workflow
from .grounding import GROUNDING_KEY, evaluate_grounding
from .loader import TranscriptInput, load_meeting
from .manifest import build_manifest, derive_run_id, manifest_to_json
from .paths import debug_filename, manifest_filename, processed_meeting_dir
from .serialize import canonical_json
from .writer import write_promoted_artifact

# Stable created_at for deterministic envelopes inside the pipeline.
# The wall-clock-free run identity is the (transcript_hash, metadata_hash,
# workflow_name) tuple captured in the manifest; the timestamp here exists
# only to satisfy the envelope schema.
_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"


def _stable_artifact_id(*, kind: str, trace_id: str, payload: dict) -> str:
    seed = compute_content_hash({"kind": kind, "trace_id": trace_id, "payload": payload})
    return f"{kind[:3]}-{seed[:24]}"


def _stabilize(artifact: Artifact, kind: str) -> Artifact:
    """Replace uuid + wall-clock fields on an artifact made inside the pipeline."""
    artifact.artifact_id = _stable_artifact_id(
        kind=kind, trace_id=artifact.trace_id, payload=artifact.payload
    )
    artifact.created_at = _DETERMINISTIC_CREATED_AT
    return artifact


@dataclass
class PipelineResult:
    run_id: str
    transcript_input: TranscriptInput
    workflow_name: str
    target: Artifact
    eval_results: list[Artifact]
    grounding_eval: Artifact
    control_decision: Artifact
    promoted: bool
    written_paths: list[str] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    debug_report: dict[str, Any] | None = None
    manifest_path: str | None = None
    debug_path: str | None = None
    store: ArtifactStore | None = None


_CONTENT_SIGNAL_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "meeting_minutes": ("decisions", "action_items", "open_questions"),
    "decision_brief": ("options", "rationale", "recommendation"),
    "agency_question_summary": ("question", "summary", "citations"),
    "meeting_action_log": ("actions",),
}


def _make_eval(target: Artifact, eval_type: str, reason_codes: list[str]) -> Artifact:
    passed = not reason_codes
    payload = {
        "eval_type": eval_type,
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
    }
    return new_artifact(
        artifact_type="eval_result",
        payload=payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


def _grounding_eval_artifact(target: Artifact, transcript_input: TranscriptInput) -> Artifact:
    _, reason_codes = evaluate_grounding(transcript_input, target.payload)
    return _make_eval(target, "source_grounding", reason_codes)


def _transcript_evidence_eval(
    target: Artifact, transcript_input: TranscriptInput
) -> Artifact:
    """Block promotion if a transcript-sourced run produced no grounded spans."""
    grounding = target.payload.get(GROUNDING_KEY) or []
    reasons = (
        ["no_transcript_evidence"]
        if transcript_input.source_type == "transcript" and not grounding
        else []
    )
    return _make_eval(target, "transcript_evidence", reasons)


def _content_signal_eval(
    target: Artifact, transcript_input: TranscriptInput
) -> Artifact:
    """For non-transcript sources, fail when every content list is empty."""
    reasons: list[str] = []
    if transcript_input.source_type in {"notes", "summary"}:
        keys = _CONTENT_SIGNAL_KEYS_BY_TYPE.get(target.artifact_type, ())
        if keys and all(
            target.payload.get(k) in (None, "", [], (), {}) for k in keys
        ):
            reasons.append("empty_content_signal")
    return _make_eval(target, "content_signal", reasons)


def _trace_id_for(transcript_input: TranscriptInput, workflow_name: str) -> str:
    seed = (
        transcript_input.transcript_hash
        + ":"
        + transcript_input.metadata_hash
        + ":"
        + workflow_name
    ).encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return f"trace-{digest[:16]}"


def run_transcript_pipeline(
    *,
    lake_root: Path | str,
    meeting_id: str | None = None,
    transcript_input: TranscriptInput | None = None,
    workflow_name: str = "meeting_minutes",
    write_outputs: bool = True,
) -> PipelineResult:
    """One end-to-end run: raw inputs -> promoted output (or explained block).

    Either meeting_id or transcript_input must be supplied. lake_root is
    always required (writes land underneath it).
    """
    if not supported_workflow(workflow_name):
        raise ValueError(f"unsupported workflow_name {workflow_name!r}")

    if transcript_input is None:
        if meeting_id is None:
            raise ValueError("must provide meeting_id or transcript_input")
        transcript_input = load_meeting(lake_root, meeting_id)

    store = ArtifactStore()
    trace_id = _trace_id_for(transcript_input, workflow_name)

    # Produce
    payload = build_grounded_payload(transcript_input, workflow_name)
    context_bundle = _stabilize(
        build_context_bundle(
            input_text=transcript_input.transcript_text,
            purpose=workflow_name,
            trace_id=trace_id,
        ),
        "context",
    )
    store.put(context_bundle)

    target = _stabilize(
        new_artifact(
            artifact_type=workflow_name,
            payload=payload,
            trace_id=trace_id,
            status="draft",
            input_refs=[context_bundle.artifact_id],
        ),
        "art",
    )
    store.put(target)

    # Evaluate (existing required evals + grounding + evidence + content)
    base_evals = run_required_evals(target)
    grounding_eval = _grounding_eval_artifact(target, transcript_input)
    evidence_eval = _transcript_evidence_eval(target, transcript_input)
    content_eval = _content_signal_eval(target, transcript_input)
    eval_results = base_evals + [grounding_eval, evidence_eval, content_eval]
    eval_results = [_stabilize(r, "evl") for r in eval_results]
    for r in eval_results:
        store.put(r)

    # Decide
    control_decision = _stabilize(decide_control(target, eval_results), "ctl")
    store.put(control_decision)

    # Promote
    promote_if_allowed(store, target, control_decision)
    promoted = target.status == "promoted"

    # Write (promoted only)
    written_paths: list[str] = []
    rejected_writes: list[dict] = []
    if write_outputs and promoted:
        written = write_promoted_artifact(lake_root, target)
        written_paths.append(str(written))
    elif not promoted:
        rejected_writes.append(
            {
                "artifact_id": target.artifact_id,
                "artifact_type": target.artifact_type,
                "reason_codes": list(control_decision.payload.get("reason_codes", [])),
            }
        )

    # Manifest + debug report
    run_id = derive_run_id(
        trace_id=trace_id,
        workflow_name=workflow_name,
        meeting_id=transcript_input.meeting_id,
    )
    manifest = build_manifest(
        transcript_input=transcript_input,
        workflow_name=workflow_name,
        produced_artifacts=[target],
        eval_artifacts=eval_results,
        control_decision=control_decision,
        promoted_artifact_ids=[target.artifact_id] if promoted else [],
        run_id=run_id,
    )
    debug_report = build_debug_report(
        run_id=run_id,
        transcript_input=transcript_input,
        workflow_name=workflow_name,
        produced_artifact=target,
        eval_results=eval_results,
        control_decision=control_decision,
        promoted=promoted,
        written_paths=written_paths,
        rejected_writes=rejected_writes,
    )

    manifest_path: str | None = None
    debug_path: str | None = None
    if write_outputs:
        target_dir = processed_meeting_dir(lake_root, transcript_input.meeting_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = str(target_dir / manifest_filename(run_id))
        debug_path = str(target_dir / debug_filename(run_id))
        Path(manifest_path).write_text(manifest_to_json(manifest), encoding="utf-8")
        Path(debug_path).write_text(canonical_json(debug_report), encoding="utf-8")

    return PipelineResult(
        run_id=run_id,
        transcript_input=transcript_input,
        workflow_name=workflow_name,
        target=target,
        eval_results=eval_results,
        grounding_eval=grounding_eval,
        control_decision=control_decision,
        promoted=promoted,
        written_paths=written_paths,
        manifest=manifest,
        debug_report=debug_report,
        manifest_path=manifest_path,
        debug_path=debug_path,
        store=store,
    )
