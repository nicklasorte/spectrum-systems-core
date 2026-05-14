"""Transcript pipeline orchestrator.

Loads raw inputs, runs the governed loop with grounding, writes promoted
artifacts, builds the manifest and debug report, and returns a small
result object that tests and the index/query layers can read.

This is the only module that ties data lake I/O to the core loop. Keeping
it in one file keeps the boundary easy to audit.

Phase Y additions:

- After the loader runs, the transcript is chunked via
  :func:`data_lake.chunker.chunk_transcript`.
- An empty chunk list or a 100%-null-speaker rate blocks the run
  fail-closed before any extraction happens.
- A ``source_record`` artifact is built and (on a healthy chunker run)
  written to disk at ``processed/meetings/<meeting_id>/source_record__
  <meeting_id>.json`` so the ``source_turn_validity`` eval can read it
  from disk every time, never from an in-memory pipeline copy.
- The target's extract function receives the chunks; each grounding
  entry gets a ``source_turns`` list and the payload's
  ``schema_version`` is set to ``"1.1.0"``.
- ``source_turn_validity`` runs alongside the other pipeline evals and
  blocks promotion when any grounding entry's ``source_turns`` does not
  resolve to a chunk in the on-disk source_record.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts import Artifact, ArtifactStore, compute_content_hash, new_artifact
from ..context import build_context_bundle
from ..control import decide_control
from ..evals import (
    run_required_evals,
    run_source_turn_validity_eval,
    SOURCE_TURN_VALIDITY_EVAL_TYPE,
)
from ..promotion import promote_if_allowed
from .chunker import (
    NO_SPEAKER_DETECTED_FINDING,
    NO_SPEAKER_STRUCTURE_FINDING,
    chunk_transcript,
    chunker_health,
)
from .debug import build_debug_report
from .extract import build_grounded_payload, supported_workflow
from .grounding import GROUNDING_KEY, evaluate_grounding
from .loader import TranscriptInput, load_meeting
from .manifest import build_manifest, derive_run_id, manifest_to_json
from .paths import debug_filename, manifest_filename, processed_meeting_dir
from .serialize import artifact_to_dict, canonical_json
from .writer import write_promoted_artifact

# Stable created_at for deterministic envelopes inside the pipeline.
# The wall-clock-free run identity is the (transcript_hash, metadata_hash,
# workflow_name) tuple captured in the manifest; the timestamp here exists
# only to satisfy the envelope schema.
_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"

# Fixed slug for the per-meeting source_record. The eval looks for
# ``source_record__<meeting_id>.json`` so the path must be predictable
# given only the meeting_id.
SOURCE_RECORD_TYPE = "source_record"


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
    source_record: Artifact | None = None
    source_record_path: str | None = None
    chunker_findings: list[str] = field(default_factory=list)


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


def _chunker_block_eval(target: Artifact, reason_codes: list[str]) -> Artifact:
    """Synthesise an eval_result that fails with the chunker's reason
    codes, so a chunker-level block flows through ``decide_control``
    rather than bypassing the loop. Reason codes encode the chunker
    finding (``no_speaker_structure`` etc.) so the operator can read
    the block reason from the eval_result alone."""
    return _make_eval(target, "chunker_health", reason_codes)


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


def _build_source_record(
    transcript_input: TranscriptInput, trace_id: str, chunks: list[dict]
) -> Artifact:
    """Build the source_record artifact for one transcript. Always
    status="promoted" once the chunker health gate passes — its job is
    to be on disk so source_turn_validity can read it. The artifact_id
    and created_at are stabilised so two runs over the same input write
    byte-identical files."""
    payload = {
        "meeting_id": transcript_input.meeting_id,
        "transcript_hash": transcript_input.transcript_hash,
        "chunks": chunks,
    }
    artifact = new_artifact(
        artifact_type=SOURCE_RECORD_TYPE,
        payload=payload,
        trace_id=trace_id,
        status="promoted",
        input_refs=[],
    )
    return _stabilize(artifact, "src")


def _write_source_record(
    lake_root: Path | str,
    transcript_input: TranscriptInput,
    source_record: Artifact,
) -> Path:
    """Write source_record to a predictable path so the eval can locate
    it: ``processed/meetings/<meeting_id>/source_record__<meeting_id>.json``.

    The fixed slug == meeting_id keeps the path deterministic; the
    canonical-JSON write keeps two runs byte-identical."""
    target_dir = processed_meeting_dir(lake_root, transcript_input.meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{SOURCE_RECORD_TYPE}__{transcript_input.meeting_id}.json"
    target_path = target_dir / filename
    target_path.write_text(
        canonical_json(artifact_to_dict(source_record)), encoding="utf-8"
    )
    return target_path


def source_record_path(
    lake_root: Path | str, meeting_id: str
) -> Path:
    """Return the on-disk path for a meeting's source_record.

    Exposed at module scope so the eval (and tests) can compute the
    path without re-deriving the slug convention."""
    return (
        processed_meeting_dir(lake_root, meeting_id)
        / f"{SOURCE_RECORD_TYPE}__{meeting_id}.json"
    )


def _build_blocked_pipeline_result(
    *,
    transcript_input: TranscriptInput,
    workflow_name: str,
    trace_id: str,
    store: ArtifactStore,
    target: Artifact,
    chunker_reason_codes: list[str],
    chunker_findings: list[str],
    write_outputs: bool,
    lake_root: Path | str,
) -> PipelineResult:
    """Build a fail-closed result when the chunker blocks the run.

    The block flows through the regular evaluator + decider so the
    manifest/debug report read like any other blocked run. No
    source_record and no target product is written."""
    chunker_eval = _stabilize(
        _chunker_block_eval(target, chunker_reason_codes), "evl"
    )
    store.put(chunker_eval)

    eval_results = [chunker_eval]
    control_decision = _stabilize(decide_control(target, eval_results), "ctl")
    store.put(control_decision)

    # Run promote_if_allowed so the target's status transitions to
    # ``rejected`` on the block — same path as the non-blocked branch.
    # Avoids draft-on-block leaking past the gate.
    promote_if_allowed(store, target, control_decision)
    promoted = target.status == "promoted"
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
        promoted_artifact_ids=[],
        run_id=run_id,
    )
    rejected_writes = [
        {
            "artifact_id": target.artifact_id,
            "artifact_type": target.artifact_type,
            "reason_codes": list(control_decision.payload.get("reason_codes", [])),
        }
    ]
    debug_report = build_debug_report(
        run_id=run_id,
        transcript_input=transcript_input,
        workflow_name=workflow_name,
        produced_artifact=target,
        eval_results=eval_results,
        control_decision=control_decision,
        promoted=promoted,
        written_paths=[],
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
        grounding_eval=chunker_eval,
        control_decision=control_decision,
        promoted=promoted,
        written_paths=[],
        manifest=manifest,
        debug_report=debug_report,
        manifest_path=manifest_path,
        debug_path=debug_path,
        store=store,
        source_record=None,
        source_record_path=None,
        chunker_findings=chunker_findings,
    )


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

    # Phase Y: chunk the transcript before any extraction. Chunker health
    # decides whether the rest of the pipeline runs.
    chunks = chunk_transcript(transcript_input.transcript_text)
    health = chunker_health(chunks)
    chunker_findings: list[str] = []
    if health.finding_code is not None:
        chunker_findings.append(health.finding_code)

    # Build context_bundle now so a chunker-block still has the
    # input_ref recorded.
    context_bundle = _stabilize(
        build_context_bundle(
            input_text=transcript_input.transcript_text,
            purpose=workflow_name,
            trace_id=trace_id,
        ),
        "context",
    )
    store.put(context_bundle)

    # Chunker-level fail-closed: empty chunks or 100%-null speaker rate.
    if not chunks or health.severity == "block":
        block_reasons: list[str] = []
        if not chunks:
            block_reasons.append("empty_chunk_list")
        if health.severity == "block" and health.finding_code:
            block_reasons.append(health.finding_code)
        # Build a stub target so the debug record names the workflow.
        target = _stabilize(
            new_artifact(
                artifact_type=workflow_name,
                payload={
                    "meeting_id": transcript_input.meeting_id,
                    "schema_version": "1.1.0",
                },
                trace_id=trace_id,
                status="draft",
                input_refs=[context_bundle.artifact_id],
            ),
            "art",
        )
        store.put(target)
        return _build_blocked_pipeline_result(
            transcript_input=transcript_input,
            workflow_name=workflow_name,
            trace_id=trace_id,
            store=store,
            target=target,
            chunker_reason_codes=block_reasons,
            chunker_findings=chunker_findings,
            write_outputs=write_outputs,
            lake_root=lake_root,
        )

    # Build + write source_record before the target evals run. The
    # source_turn_validity eval reads source_record from disk, so it
    # must exist on disk regardless of write_outputs. The on-disk file
    # is the trust anchor — never an in-memory snapshot.
    source_record = _build_source_record(transcript_input, trace_id, chunks)
    store.put(source_record)
    sr_path = _write_source_record(lake_root, transcript_input, source_record)

    # Produce target payload (chunks attach source_turns to grounding
    # entries; schema_version becomes "1.1.0").
    payload = build_grounded_payload(
        transcript_input, workflow_name, chunks=chunks
    )

    target = _stabilize(
        new_artifact(
            artifact_type=workflow_name,
            payload=payload,
            trace_id=trace_id,
            status="draft",
            input_refs=[context_bundle.artifact_id, source_record.artifact_id],
        ),
        "art",
    )
    store.put(target)

    # Evaluate: existing required evals + grounding + evidence + content
    # + source_turn_validity (always runs at 1.1.0 since the pipeline
    # always emits 1.1.0 when the chunker succeeded).
    base_evals = run_required_evals(target)
    grounding_eval = _grounding_eval_artifact(target, transcript_input)
    evidence_eval = _transcript_evidence_eval(target, transcript_input)
    content_eval = _content_signal_eval(target, transcript_input)
    turn_validity_eval = run_source_turn_validity_eval(target, sr_path)
    eval_results = base_evals + [
        grounding_eval,
        evidence_eval,
        content_eval,
        turn_validity_eval,
    ]
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
        source_record=source_record,
        source_record_path=str(sr_path),
        chunker_findings=chunker_findings,
    )
