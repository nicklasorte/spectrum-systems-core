"""Phase O.3: per-transcript pipeline run summary.

Aggregates ``orchestration_result`` artifacts (one per transcript)
plus health findings into:

1. A ``pipeline_run_summary`` artifact written to the data lake. The
   pipeline run diff tool (O.5) consumes this artifact to compute
   between-run deltas.
2. A markdown table appended to ``$GITHUB_STEP_SUMMARY``. The same
   markdown is printed to stdout when running outside GitHub Actions,
   so the summary still surfaces in a local terminal.

The module is invoked from the new ``post-pipeline`` workflow step in
``run-pipeline.yml`` after the per-transcript matrix completes. It runs
even when individual transcript jobs failed (``if: always()``) and
silently skips transcripts that did not produce an orchestration
artifact -- the missing rows are rendered as ``-- (no orchestration
artifact)`` so a partial run is visible without crashing the summary.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from .finding import HealthFinding

_LOG = logging.getLogger(__name__)


SCHEMA_VERSION: str = "1.0.0"
ARTIFACT_TYPE: str = "pipeline_run_summary"

# Markdown layout truncates long source_ids in the rendered table only;
# the artifact carries the full id.
_MARKDOWN_SOURCE_ID_LIMIT: int = 40


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _orchestration_dir(data_lake_path: Union[str, Path]) -> Path:
    return (
        Path(data_lake_path)
        / "store"
        / "artifacts"
        / "orchestration"
    )


def _load_orchestration_artifacts(
    data_lake_path: Union[str, Path],
) -> List[Dict[str, Any]]:
    """Read every ``*_extraction.json`` orchestration artifact."""
    dir_ = _orchestration_dir(data_lake_path)
    out: List[Dict[str, Any]] = []
    if not dir_.is_dir():
        return out
    for path in sorted(dir_.glob("*_extraction.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("orchestration_unreadable: %s: %s", path, exc)
            continue
        if isinstance(rec, dict) and rec.get("artifact_type") == "orchestration_result":
            out.append(rec)
    return out


def _load_health_findings(
    data_lake_path: Union[str, Path],
) -> List[Dict[str, Any]]:
    health_dir = (
        Path(data_lake_path) / "store" / "artifacts" / "health"
    )
    out: List[Dict[str, Any]] = []
    if not health_dir.is_dir():
        return out
    for path in sorted(health_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("artifact_type") == "health_finding":
            out.append(rec)
    return out


def _aggregate_findings(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[tuple, int] = {}
    for f in findings:
        code = str(f.get("finding_code") or "")
        sev = str(f.get("severity") or "")
        if not code or not sev:
            continue
        key = (code, sev)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"finding_code": code, "severity": sev, "count": n}
        for (code, sev), n in sorted(counts.items())
    ]


def _row_for_source(
    source_id: str,
    orch_by_source: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    orch = orch_by_source.get(source_id)
    if orch is None:
        return {
            "source_id": source_id,
            "chunks_attempted": 0,
            "chunks_succeeded": 0,
            "chunks_blocked": 0,
            "stage_status": "missing",
            "has_orchestration_artifact": False,
            "block_reasons": {},
            "synthesize_ok": None,
        }
    return {
        "source_id": source_id,
        "chunks_attempted": int(orch.get("chunks_attempted", 0) or 0),
        "chunks_succeeded": int(orch.get("chunks_succeeded", 0) or 0),
        "chunks_blocked": int(orch.get("chunks_blocked", 0) or 0),
        "stage_status": str(orch.get("stage_status") or "missing"),
        "has_orchestration_artifact": True,
        "block_reasons": dict(orch.get("block_reasons") or {}),
        "synthesize_ok": None,
    }


def build_summary(
    data_lake_path: Union[str, Path],
    pipeline_run_id: str,
    *,
    source_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Build the ``pipeline_run_summary`` artifact dict."""
    orchestrations = _load_orchestration_artifacts(data_lake_path)
    orch_by_source: Dict[str, Dict[str, Any]] = {}
    for orch in orchestrations:
        sid = str(orch.get("source_id") or "")
        if not sid:
            continue
        # Last write wins (sorted alphabetically by run_id-prefixed
        # filename; deterministic and good-enough).
        orch_by_source[sid] = orch

    if source_ids is None:
        # Default: every source_id with an orchestration artifact.
        source_ids_list = sorted(orch_by_source.keys())
    else:
        source_ids_list = list(source_ids)

    transcripts: List[Dict[str, Any]] = [
        _row_for_source(sid, orch_by_source) for sid in source_ids_list
    ]

    total_attempted = sum(t["chunks_attempted"] for t in transcripts)
    total_succeeded = sum(t["chunks_succeeded"] for t in transcripts)
    total_blocked = sum(t["chunks_blocked"] for t in transcripts)
    block_reason_breakdown: Dict[str, int] = {}
    for t in transcripts:
        for reason, count in t["block_reasons"].items():
            block_reason_breakdown[reason] = (
                block_reason_breakdown.get(reason, 0) + int(count or 0)
            )

    findings = _load_health_findings(data_lake_path)

    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "pipeline_run_id": pipeline_run_id,
        "created_at": _now_iso(),
        "transcripts": transcripts,
        "totals": {
            "transcripts": len(transcripts),
            "chunks_attempted": total_attempted,
            "chunks_succeeded": total_succeeded,
            "chunks_blocked": total_blocked,
            "blocked_rate": (
                float(total_blocked) / float(total_attempted)
                if total_attempted > 0
                else 0.0
            ),
        },
        "block_reason_breakdown": block_reason_breakdown,
        "health_findings": _aggregate_findings(findings),
    }


_SEVERITY_GLYPH = {"halt": "🛑", "warn": "⚠️", "info": "ℹ️"}
_STAGE_GLYPH = {"ok": "clm✓", "partial": "clm⚠", "failed": "clm✗", "missing": "—"}


def _truncate_for_markdown(value: str, limit: int = _MARKDOWN_SOURCE_ID_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def render_markdown(summary: Dict[str, Any]) -> str:
    """Render the summary as a GitHub-flavoured markdown block."""
    lines: List[str] = []
    pipeline_run_id = summary.get("pipeline_run_id") or "<unknown>"
    created_at = summary.get("created_at") or ""
    lines.append(
        f"## Pipeline Run Summary — {created_at} (run {pipeline_run_id})"
    )
    lines.append("")
    lines.append(
        "| Transcript | Chunks | Succeeded | Blocked | Stage | Notes |"
    )
    lines.append("|---|---|---|---|---|---|")
    for row in summary.get("transcripts") or []:
        sid = _truncate_for_markdown(row.get("source_id") or "")
        if not row.get("has_orchestration_artifact"):
            lines.append(
                f"| {sid} | — | — | — | — | — (no orchestration artifact) |"
            )
            continue
        stage = _STAGE_GLYPH.get(row.get("stage_status") or "", "?")
        lines.append(
            "| {sid} | {att} | {ok} | {bl} | {stage} | |".format(
                sid=sid,
                att=row.get("chunks_attempted", 0),
                ok=row.get("chunks_succeeded", 0),
                bl=row.get("chunks_blocked", 0),
                stage=stage,
            )
        )
    totals = summary.get("totals") or {}
    lines.append("")
    lines.append(
        "**Total:** "
        f"{totals.get('transcripts', 0)} transcripts | "
        f"{totals.get('chunks_attempted', 0)} chunks attempted | "
        f"{totals.get('chunks_succeeded', 0)} succeeded | "
        f"{totals.get('chunks_blocked', 0)} blocked"
    )

    breakdown = summary.get("block_reason_breakdown") or {}
    if breakdown:
        lines.append("")
        lines.append("### Blocked chunk breakdown")
        lines.append("| Block reason | Count |")
        lines.append("|---|---|")
        for reason, count in sorted(breakdown.items()):
            if int(count or 0) > 0:
                lines.append(f"| {reason} | {count} |")

    findings = summary.get("health_findings") or []
    if findings:
        lines.append("")
        lines.append("### Health findings")
        lines.append("| Finding | Severity | Count |")
        lines.append("|---|---|---|")
        for f in findings:
            sev = f.get("severity") or ""
            glyph = _SEVERITY_GLYPH.get(sev, "")
            lines.append(
                f"| {f.get('finding_code', '')} | {glyph} {sev} | {f.get('count', 1)} |"
            )

    return "\n".join(lines) + "\n"


def write_artifact(
    summary: Dict[str, Any],
    *,
    data_lake_path: Union[str, Path],
) -> Optional[Path]:
    """Persist ``summary`` as a ``pipeline_run_summary`` artifact.

    The artifact lives under
    ``<data_lake>/store/artifacts/pipeline_runs/<pipeline_run_id>.json``.
    Returns the write path on success, ``None`` otherwise.
    """
    run_id = summary.get("pipeline_run_id") or ""
    if not run_id:
        _LOG.warning("pipeline_run_summary_missing_run_id")
        return None
    target_dir = (
        Path(data_lake_path) / "store" / "artifacts" / "pipeline_runs"
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("pipeline_run_summary_mkdir_failed: %s", exc)
        return None
    target = target_dir / f"{run_id}.json"
    try:
        target.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("pipeline_run_summary_write_failed: %s", exc)
        return None

    try:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )
        try:
            validate_artifact(summary, ARTIFACT_TYPE)
        except (ArtifactValidationError, SchemaNotFoundError) as exc:
            _LOG.warning("pipeline_run_summary_schema_violation: %s", exc)
    except ImportError:
        pass

    return target


def emit_step_summary(markdown: str) -> None:
    """Append ``markdown`` to ``$GITHUB_STEP_SUMMARY`` or stdout.

    Falls back to stdout when the env var is unset so a local invocation
    still surfaces the summary.
    """
    gh_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if gh_path:
        try:
            with open(gh_path, "a", encoding="utf-8") as fh:
                fh.write(markdown)
            return
        except OSError as exc:
            _LOG.warning("github_step_summary_append_failed: %s", exc)
    sys.stdout.write(markdown)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectrum_systems_core.health.run_summary",
        description=(
            "Build the per-transcript pipeline run summary and emit the "
            "markdown table to $GITHUB_STEP_SUMMARY (or stdout)."
        ),
    )
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--pipeline-run-id", required=True)
    parser.add_argument(
        "--output-format",
        choices=("github_actions_summary", "markdown", "json"),
        default="github_actions_summary",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=None,
        help="Optional: restrict the summary to specific source_ids.",
    )
    args = parser.parse_args(argv)

    summary = build_summary(
        args.data_lake,
        args.pipeline_run_id,
        source_ids=args.source_id,
    )
    write_artifact(summary, data_lake_path=args.data_lake)

    if args.output_format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    md = render_markdown(summary)
    if args.output_format == "github_actions_summary":
        emit_step_summary(md)
        return 0
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
