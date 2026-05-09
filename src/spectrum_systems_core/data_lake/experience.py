"""Harness experience records (SSC-032).

A single, compressed record per workflow run that captures what happened
without granting authority. Inspired by Stanford / Meta-Harness style
experience replay, but with autonomy explicitly stripped out:

- No autonomous harness optimization.
- No self-modifying code or prompts.
- No model calls. Records are filesystem artifacts.

File: `processed/meetings/<meeting_id>/experience_history.jsonl`
- One JSON object per line, deterministic field order via canonical_json.
- Sorted by `(workflow_name, experience_id)`. Two runs over the same
  inputs produce a byte-identical file.

Distinguished from `run_history.jsonl` (SSC-031):
- `experience_history` is the "what happened" projection. Its
  identifying fields are `experience_id`, `input_hash`, `output_hash`,
  `human_readable_summary` — a compressed lesson.
- `run_history` is the "where to look" projection (`manifest_path`,
  `debug_path`, `run_markdown_path`).

M4 in `ssc_next_memory_redteam_2.md`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .markdown import explain_reason_codes
from .paths import processed_meeting_dir
from .pipeline import PipelineResult
from .serialize import canonical_json

EXPERIENCE_HISTORY_FILENAME = "experience_history.jsonl"
EXPERIENCE_SCHEMA_VERSION = 1


def experience_history_path(lake_root: Path | str, meeting_id: str) -> Path:
    return (
        processed_meeting_dir(lake_root, meeting_id) / EXPERIENCE_HISTORY_FILENAME
    )


def _experience_id_for(result: PipelineResult) -> str:
    """Deterministic id derived from the run's identity tuple."""
    seed = (
        f"{result.transcript_input.transcript_hash}:"
        f"{result.transcript_input.metadata_hash}:"
        f"{result.workflow_name}"
    ).encode("utf-8")
    return f"exp-{hashlib.sha256(seed).hexdigest()[:16]}"


def _input_hash(result: PipelineResult) -> str:
    return result.transcript_input.transcript_hash


def _output_hash(result: PipelineResult) -> str | None:
    if not result.promoted:
        return None
    return result.target.content_hash


def _eval_summary(result: PipelineResult) -> dict[str, Any]:
    passed = sorted(
        e.payload.get("eval_type")
        for e in result.eval_results
        if e.payload.get("status") == "pass" and e.payload.get("eval_type")
    )
    failed = sorted(
        e.payload.get("eval_type")
        for e in result.eval_results
        if e.payload.get("status") == "fail" and e.payload.get("eval_type")
    )
    return {"passed": passed, "failed": failed}


def _human_readable_summary(result: PipelineResult) -> str:
    if result.promoted:
        return (
            f"workflow {result.workflow_name!r} promoted artifact "
            f"{result.target.artifact_id!r}"
        )
    reasons = list(result.control_decision.payload.get("reason_codes", []))
    explanation = explain_reason_codes(reasons) if reasons else "no reason codes"
    return (
        f"workflow {result.workflow_name!r} blocked: "
        f"{', '.join(reasons) if reasons else '(none)'} ({explanation})"
    )


def build_experience_record(result: PipelineResult) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "experience_id": _experience_id_for(result),
        "meeting_id": result.transcript_input.meeting_id,
        "workflow_name": result.workflow_name,
        "input_hash": _input_hash(result),
        "output_hash": _output_hash(result),
        "decision": result.control_decision.payload.get("decision"),
        "eval_summary": _eval_summary(result),
        "reason_codes": list(
            result.control_decision.payload.get("reason_codes", [])
        ),
        "artifact_type": result.target.artifact_type,
        "artifact_id": result.target.artifact_id,
        "human_readable_summary": _human_readable_summary(result),
    }
    return record


def write_experience_history(
    lake_root: Path | str,
    *,
    meeting_id: str,
    records: list[dict[str, Any]],
) -> Path:
    out = experience_history_path(lake_root, meeting_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sorted(
        records,
        key=lambda r: (r.get("workflow_name", ""), r.get("experience_id", "")),
    )
    out.write_text(
        "".join(canonical_json(r) for r in sorted_records),
        encoding="utf-8",
    )
    return out
