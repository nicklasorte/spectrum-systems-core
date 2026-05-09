"""Debuggability report.

One JSON object per run. Plain English fields. The goal is that a new
engineer can read this file and explain what happened without opening any
source.

A debug report is not a product artifact and not subject to the promotion
rule.
"""
from __future__ import annotations

from typing import Any

DEBUG_SCHEMA_VERSION = 2


def _eval_summary(eval_results: list) -> dict[str, Any]:
    passed: list[dict] = []
    failed: list[dict] = []
    for r in eval_results:
        entry = {
            "eval_type": r.payload.get("eval_type"),
            "artifact_id": r.artifact_id,
            "reason_codes": list(r.payload.get("reason_codes", [])),
        }
        if r.payload.get("status") == "pass":
            passed.append(entry)
        else:
            failed.append(entry)
    return {"passed": passed, "failed": failed}


_INSPECTION_HINTS: dict[str, str] = {
    "failed:transcript_evidence": (
        "open transcript.txt and confirm the workflow's prefix lines exist"
    ),
    "failed:source_grounding": (
        "open transcript.txt and confirm grounding excerpts match"
    ),
    "failed:non_empty_payload": (
        "review the workflow extractor and the transcript shape"
    ),
    "failed:content_signal": (
        "the source is a notes/summary input but extracted no content"
    ),
    "missing_required_evals": (
        "no eval_result artifacts were produced; check the loop wiring"
    ),
}


def _explain_reason_for_inspection(reason_code: str) -> str:
    if reason_code in _INSPECTION_HINTS:
        return _INSPECTION_HINTS[reason_code]
    if reason_code.startswith("empty_required_field:"):
        field = reason_code.split(":", 1)[1]
        return (
            f"workflow extractor produced an empty value for required "
            f"field '{field}'; check transcript prefix lines"
        )
    if reason_code.startswith("missing_field:"):
        field = reason_code.split(":", 1)[1]
        return f"required field '{field}' was missing from the produced payload"
    return f"see eval reason code '{reason_code}'"


def _build_inspect_next(
    reason_codes: list[str],
    failed_eval_reason_codes: list[str],
    promoted: bool,
) -> list[str]:
    """Plain-English next-step hints for a blocked run.

    Both control's `reason_codes` and the per-eval `reason_codes` are
    surfaced so a granular code like `empty_required_field:agency` is
    visible even when control's compact form is `failed:non_empty_payload`.
    """
    if promoted:
        return []
    seen: list[str] = []
    for code in reason_codes + failed_eval_reason_codes:
        if code not in seen:
            seen.append(code)
    if not seen:
        return [
            "no reason codes were emitted; inspect the eval results below"
        ]
    return [_explain_reason_for_inspection(rc) for rc in seen]


def build_debug_report(
    *,
    run_id: str,
    transcript_input,
    workflow_name: str,
    produced_artifact,
    eval_results: list,
    control_decision,
    promoted: bool,
    written_paths: list[str],
    rejected_writes: list[dict] | None = None,
) -> dict[str, Any]:
    """Plain summary of one run. Always built, even on block.

    Field order is deliberately top-to-bottom debuggable: a new engineer
    can answer the standard questions in sequence.
    """
    decision = control_decision.payload.get("decision")
    reason_codes = list(control_decision.payload.get("reason_codes", []))
    eval_summary = _eval_summary(eval_results)
    failed_eval_reason_codes: list[str] = []
    for ev in eval_results:
        if ev.payload.get("status") == "fail":
            for rc in ev.payload.get("reason_codes", []):
                if rc not in failed_eval_reason_codes:
                    failed_eval_reason_codes.append(rc)

    if promoted:
        explanation = (
            f"control allowed because all required evals passed: "
            f"{[e['eval_type'] for e in eval_summary['passed']]}"
        )
        outcome = "promoted"
    else:
        explanation = (
            f"control {decision} because of reason codes: {reason_codes}"
        )
        outcome = "rejected"

    optional_metadata = {
        k: v
        for k, v in transcript_input.metadata.items()
        if k not in ("meeting_id", "title", "date", "source_type")
    }

    failure_path = {
        "input_loaded": True,
        "workflow_ran": True,
        "artifact_produced": produced_artifact is not None,
        "eval_failed": [e["eval_type"] for e in eval_summary["failed"]],
        "control_decision": decision,
        "json_written": promoted and bool(written_paths),
    }

    return {
        "schema_version": DEBUG_SCHEMA_VERSION,
        "run_id": run_id,
        "trace_id": produced_artifact.trace_id,
        "meeting_id": transcript_input.meeting_id,
        "workflow_name": workflow_name,
        "input": {
            "transcript_path": transcript_input.transcript_path,
            "metadata_path": transcript_input.metadata_path,
            "transcript_hash": transcript_input.transcript_hash,
            "metadata_hash": transcript_input.metadata_hash,
            "title": transcript_input.title,
            "date": transcript_input.date,
            "source_type": transcript_input.source_type,
            "optional_metadata": optional_metadata,
        },
        "produced_artifact": {
            "artifact_id": produced_artifact.artifact_id,
            "artifact_type": produced_artifact.artifact_type,
            "content_hash": produced_artifact.content_hash,
            "status": produced_artifact.status,
        },
        "evals": eval_summary,
        "control": {
            "artifact_id": control_decision.artifact_id,
            "decision": decision,
            "reason_codes": reason_codes,
            "explanation": explanation,
        },
        "outcome": outcome,
        "written_paths": sorted(written_paths),
        "rejected_writes": list(rejected_writes or []),
        "failure_path": failure_path,
        "inspect_next": _build_inspect_next(
            reason_codes, failed_eval_reason_codes, promoted
        ),
    }
