"""eval_history.jsonl projection for the live-LLM workflow.

The data-lake contract (§6.4) pins one ``eval_history.jsonl`` per
meeting: one JSON object per line, deterministic field order via
``serialize.canonical_json``, sorted on a stable key, harness-memory
(never authority). ``data_lake/eval_history.py`` produces it from a
``PipelineResult``; the LLM workflow does not run the deterministic
pipeline, so this module produces a SHAPE-IDENTICAL projection from a
``WorkflowResult``.

Shape identity is deliberate: a reader cannot tell which producer wrote
a given row. In particular ``reason_codes`` is projected verbatim, so
the GT-coverage eval's ``coverage_threshold:<v>`` token lands here and
the threshold used on every run is auditable from this file alone —
satisfying the Step 6 "written into eval_history.jsonl" requirement
without coupling the LLM path to the determinism-critical pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..data_lake.serialize import canonical_json
from .meeting_minutes import WorkflowResult

EVAL_HISTORY_FILENAME = "eval_history.jsonl"
EVAL_HISTORY_SCHEMA_VERSION = 1


def _coerce_score(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def build_eval_records(
    result: WorkflowResult, *, meeting_id: str, workflow_name: str
) -> list[dict[str, Any]]:
    """Project ``result.eval_results`` into eval_history rows.

    Same key set, same ``_coerce_score`` rule, and same record order
    fields as ``data_lake.eval_history.build_eval_records`` so the two
    producers cannot drift.
    """
    records: list[dict[str, Any]] = []
    for ev in result.eval_results:
        payload = ev.payload
        records.append(
            {
                "schema_version": EVAL_HISTORY_SCHEMA_VERSION,
                "meeting_id": meeting_id,
                "workflow_name": workflow_name,
                "artifact_type": result.meeting_minutes.artifact_type,
                "eval_type": payload.get("eval_type"),
                "status": payload.get("status"),
                "score": _coerce_score(payload.get("score")),
                "reason_codes": list(payload.get("reason_codes", [])),
                "target_artifact_id": payload.get("target_artifact_id")
                or result.meeting_minutes.artifact_id,
            }
        )
    return records


def write_eval_history(
    lake_root: Path | str,
    *,
    source_id: str,
    records: list[dict[str, Any]],
) -> Path:
    """Write the projection next to the GT data under ``store/``.

    The path mirrors the ``store/``-rooted layout the GT pairs use
    (``scripts/create_human_gt_pairs._output_path``), NOT the core
    pipeline's ``processed_meeting_dir`` layout — the LLM path's
    auditability files live with the LLM path's inputs. Deterministic:
    sorted records, canonical JSON, byte-identical on re-run.
    """
    out = (
        Path(lake_root)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / EVAL_HISTORY_FILENAME
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sorted(
        records,
        key=lambda r: (
            r.get("workflow_name", ""),
            r.get("eval_type", "") or "",
            r.get("target_artifact_id", ""),
        ),
    )
    out.write_text(
        "".join(canonical_json(r) for r in sorted_records),
        encoding="utf-8",
    )
    return out


__all__ = [
    "EVAL_HISTORY_FILENAME",
    "build_eval_records",
    "write_eval_history",
]
