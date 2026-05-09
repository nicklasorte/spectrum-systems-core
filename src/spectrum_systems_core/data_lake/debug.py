"""Debuggability report.

One JSON object per run. Plain English fields. The goal is that a new
engineer can read this file and explain what happened without opening any
source.

A debug report is not a product artifact and not subject to the promotion
rule.
"""
from __future__ import annotations

from typing import Any

DEBUG_SCHEMA_VERSION = 1


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
    """Plain summary of one run. Always built, even on block."""
    decision = control_decision.payload.get("decision")
    reason_codes = list(control_decision.payload.get("reason_codes", []))
    eval_summary = _eval_summary(eval_results)

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
    }
