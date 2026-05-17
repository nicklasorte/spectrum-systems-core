"""Run history: one record per workflow run, written to a meeting-level JSONL.

SSC-031. This is harness memory, not authority. It does not change
control behavior, never blocks promotion, and never feeds back into the
loop. The control function and promotion gate continue to be the only
sources of trust.

File: `processed/meetings/<meeting_id>/run_history.jsonl`
- One JSON object per line, deterministic field order via canonical_json.
- Sorted by `(workflow_name, run_id)` so two runs over the same inputs
  produce a byte-identical file.

Distinguished from `experience_history.jsonl` (SSC-032):
- `run_history` is a "where to look" projection. Its identifying
  fields are `run_id`, `manifest_path`, `debug_path`,
  `run_markdown_path` — pointers at canonical run records.
- `experience_history` is a "what happened" projection. Its identifying
  fields are `experience_id`, `input_hash`, `output_hash`,
  `human_readable_summary` — a compressed lesson.

The two files are not redundant; deleting either would lose a real
debuggability surface. M4 in `ssc_next_memory_redteam_2.md`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .markdown import (
    RUNS_SUBDIR,
    explain_reason_codes,
    run_markdown_filename,
    runs_markdown_dir,
)
from .paths import processed_meeting_dir
from .pipeline import PipelineResult
from .serialize import canonical_json

RUN_HISTORY_FILENAME = "run_history.jsonl"
RUN_HISTORY_SCHEMA_VERSION = 1


def run_history_path(lake_root: Path | str, meeting_id: str) -> Path:
    return processed_meeting_dir(lake_root, meeting_id) / RUN_HISTORY_FILENAME


def build_run_record(
    result: PipelineResult, *, run_markdown_path: str | None = None
) -> dict[str, Any]:
    """Build a single run history record from one pipeline run."""
    eval_types = sorted(
        {
            e.payload.get("eval_type")
            for e in result.eval_results
            if e.payload.get("eval_type")
        }
    )
    decision = result.control_decision.payload.get("decision")
    reason_codes = list(
        result.control_decision.payload.get("reason_codes", [])
    )
    record: dict[str, Any] = {
        "schema_version": RUN_HISTORY_SCHEMA_VERSION,
        "run_id": result.run_id,
        "meeting_id": result.transcript_input.meeting_id,
        "workflow_name": result.workflow_name,
        "target_artifact_type": result.target.artifact_type,
        "decision": decision,
        "promoted": result.promoted,
        "reason_codes": reason_codes,
        "eval_types": eval_types,
        "artifact_id": result.target.artifact_id,
        "manifest_path": result.manifest_path,
        "debug_path": result.debug_path,
        "run_markdown_path": run_markdown_path,
        # `created_at` on the produced artifact is deterministic
        # (`_DETERMINISTIC_CREATED_AT` in pipeline.py) so it is safe to
        # include in a record we want byte-stable across runs.
        "created_at": result.target.created_at,
    }
    return record


def write_run_history(
    lake_root: Path | str,
    *,
    meeting_id: str,
    records: list[dict[str, Any]],
) -> Path:
    """Write run_history.jsonl deterministically. Returns the path."""
    out = run_history_path(lake_root, meeting_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sorted(
        records, key=lambda r: (r.get("workflow_name", ""), r.get("run_id", ""))
    )
    out.write_text(
        "".join(canonical_json(r) for r in sorted_records),
        encoding="utf-8",
    )
    return out


def render_run_note_markdown(record: dict[str, Any]) -> str:
    """Per-run plain-text note. Lives under markdown/runs/<run_id>.md."""
    lines: list[str] = ["---"]
    for key in (
        "artifact_type",
        "meeting_id",
        "run_id",
        "workflow_name",
        "decision",
        "promoted",
        "status",
        "canonical",
    ):
        if key == "artifact_type":
            lines.append("artifact_type: run_note")
            continue
        if key == "status":
            lines.append("status: view")
            continue
        if key == "canonical":
            lines.append("canonical: false")
            continue
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    front = "\n".join(lines) + "\n"

    body: list[str] = [
        "",
        f"# Run `{record.get('run_id', '?')}`",
        "",
        f"- Meeting: [meeting index](../index.md) — "
        f"[[Meeting/{record.get('meeting_id', '?')}]]",
        f"- Workflow: `{record.get('workflow_name', '?')}`",
        f"- Decision: `{record.get('decision', '?')}` "
        f"(promoted: {'yes' if record.get('promoted') else 'no'})",
    ]
    reason_codes = record.get("reason_codes") or []
    if reason_codes:
        body.append(
            f"- Reason codes: `{', '.join(reason_codes)}` "
            f"({explain_reason_codes(reason_codes)})"
        )
    eval_types = record.get("eval_types") or []
    if eval_types:
        body.append(f"- Evals run: {', '.join(eval_types)}")

    manifest_path = record.get("manifest_path")
    if manifest_path:
        manifest_name = Path(manifest_path).name
        body.append(
            f"- Manifest (canonical run record): "
            f"[{manifest_name}](../../{manifest_name})"
        )
    debug_path = record.get("debug_path")
    if debug_path:
        debug_name = Path(debug_path).name
        body.append(
            f"- Debug report (canonical): "
            f"[{debug_name}](../../{debug_name})"
        )

    body.append("")
    body.append(
        "> Run notes are regenerated views. The canonical run records "
        "are the JSON manifest and debug report linked above."
    )
    body.append("")
    return front + "\n".join(body) + "\n"


def write_run_note_markdown(
    lake_root: Path | str, *, meeting_id: str, record: dict[str, Any]
) -> Path:
    out_dir = runs_markdown_dir(lake_root, meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = record.get("run_id") or "unknown"
    path = out_dir / run_markdown_filename(run_id)
    path.write_text(render_run_note_markdown(record), encoding="utf-8")
    return path


def run_note_markdown_relpath(run_id: str) -> str:
    """Path used inside run_history.jsonl to point at the run note md."""
    return f"{RUNS_SUBDIR}/{run_markdown_filename(run_id)}"
