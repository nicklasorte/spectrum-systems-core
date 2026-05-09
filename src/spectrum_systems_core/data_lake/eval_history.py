"""Eval score history (SSC-033).

Per-meeting JSONL listing every eval that ran in every workflow. The
records are derived from `eval_result` artifacts produced inside the
existing pipeline; this file only re-projects them in a stable shape so
a human can scan eval outcomes without opening each manifest.

File: `processed/meetings/<meeting_id>/eval_history.jsonl`
- One JSON object per line, deterministic field order via canonical_json.
- Sorted by `(workflow_name, eval_type, target_artifact_id)`.
- This file is harness memory, not authority. The control function
  remains the only thing that can block a promotion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import processed_meeting_dir
from .pipeline import PipelineResult
from .serialize import canonical_json

EVAL_HISTORY_FILENAME = "eval_history.jsonl"
EVAL_HISTORY_SCHEMA_VERSION = 1


def eval_history_path(lake_root: Path | str, meeting_id: str) -> Path:
    return processed_meeting_dir(lake_root, meeting_id) / EVAL_HISTORY_FILENAME


def _coerce_score(raw: Any) -> float | None:
    """Score is a float in `{0.0, 1.0}` or `None`.

    M5 in `ssc_next_memory_redteam_2.md`: a future eval that emits a
    different shape (string, bucket, etc.) must not silently land in
    the JSONL. Coerce non-numeric values to `None` so a reader can
    distinguish "score not produced" from "score produced".
    """
    if isinstance(raw, bool):  # bool is a subclass of int; reject explicitly
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def build_eval_records(result: PipelineResult) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for ev in result.eval_results:
        payload = ev.payload
        records.append(
            {
                "schema_version": EVAL_HISTORY_SCHEMA_VERSION,
                "meeting_id": result.transcript_input.meeting_id,
                "workflow_name": result.workflow_name,
                "artifact_type": result.target.artifact_type,
                "eval_type": payload.get("eval_type"),
                "status": payload.get("status"),
                "score": _coerce_score(payload.get("score")),
                "reason_codes": list(payload.get("reason_codes", [])),
                "target_artifact_id": payload.get("target_artifact_id")
                or result.target.artifact_id,
            }
        )
    return records


def write_eval_history(
    lake_root: Path | str,
    *,
    meeting_id: str,
    records: list[dict[str, Any]],
) -> Path:
    out = eval_history_path(lake_root, meeting_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sorted(
        records,
        key=lambda r: (
            r.get("workflow_name", ""),
            r.get("eval_type", ""),
            r.get("target_artifact_id", ""),
        ),
    )
    out.write_text(
        "".join(canonical_json(r) for r in sorted_records),
        encoding="utf-8",
    )
    return out
