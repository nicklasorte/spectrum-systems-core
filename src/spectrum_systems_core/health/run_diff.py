"""Phase O.5: diff two ``pipeline_run_summary`` artifacts.

Given two run ids (``run_a_id``, ``run_b_id``), this module:

1. Loads each summary from
   ``<data_lake>/store/artifacts/pipeline_runs/<run_id>.json``.
2. Computes a ``pipeline_run_diff`` artifact (transcripts, totals,
   block_reason breakdown, health findings, optional eval metrics).
3. Renders a markdown summary suitable for ``$GITHUB_STEP_SUMMARY``.

The diff is read-only over the lake. Missing summaries cause the tool
to halt with a ``pipeline_run_summary_missing`` health finding and a
non-zero exit, so a typo'd run id cannot silently produce a misleading
zero-delta artifact.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .finding import HealthFinding, write_finding
from .run_summary import emit_step_summary

_LOG = logging.getLogger(__name__)


ARTIFACT_TYPE: str = "pipeline_run_diff"
SCHEMA_VERSION: str = "1.0.0"

# Markdown source_id truncation limit (mirrors run_summary.py).
_MARKDOWN_SOURCE_ID_LIMIT: int = 40

_STAGE_RANK = {"missing": -1, "failed": 0, "partial": 1, "ok": 2}


class PipelineRunSummaryMissing(LookupError):
    """Raised when a requested pipeline_run_summary artifact does not exist."""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _summary_path(data_lake_path: str | Path, run_id: str) -> Path:
    return (
        Path(data_lake_path)
        / "store"
        / "artifacts"
        / "pipeline_runs"
        / f"{run_id}.json"
    )


def _load_summary(
    data_lake_path: str | Path,
    run_id: str,
) -> dict[str, Any] | None:
    path = _summary_path(data_lake_path, run_id)
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("pipeline_run_summary_unreadable: %s: %s", path, exc)
        return None
    if not isinstance(rec, dict):
        return None
    return rec


def _index_transcripts(
    summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        str(t.get("source_id") or ""): t
        for t in summary.get("transcripts") or []
        if isinstance(t, dict)
    }


def _stage_status(t: dict[str, Any] | None) -> str | None:
    if not isinstance(t, dict):
        return None
    return t.get("stage_status")


def _stage_improved(a: str | None, b: str | None) -> bool:
    ra = _STAGE_RANK.get(a or "missing", -1)
    rb = _STAGE_RANK.get(b or "missing", -1)
    return rb > ra


def _findings_set(summary: dict[str, Any]) -> list[str]:
    return [
        str(f.get("finding_code") or "")
        for f in summary.get("health_findings") or []
        if isinstance(f, dict) and f.get("finding_code")
    ]


def _eval_metrics(summary: dict[str, Any]) -> tuple[float | None, float | None]:
    em = summary.get("eval_metrics")
    if not isinstance(em, dict):
        return None, None
    cov = em.get("aggregate_coverage")
    prec = em.get("aggregate_precision")
    return (
        float(cov) if isinstance(cov, (int, float)) else None,
        float(prec) if isinstance(prec, (int, float)) else None,
    )


def diff_runs(
    run_a_id: str,
    run_b_id: str,
    data_lake_path: str | Path,
) -> dict[str, Any]:
    """Compute a ``pipeline_run_diff`` between two summary artifacts.

    Raises :class:`PipelineRunSummaryMissing` if either summary is
    absent. The caller (``main``) catches the exception, writes a
    ``pipeline_run_summary_missing`` health finding, and exits 1.
    """
    summary_a = _load_summary(data_lake_path, run_a_id)
    summary_b = _load_summary(data_lake_path, run_b_id)
    missing: list[str] = []
    if summary_a is None:
        missing.append(run_a_id)
    if summary_b is None:
        missing.append(run_b_id)
    if missing:
        raise PipelineRunSummaryMissing(
            f"pipeline_run_summary not found for run_id(s): {missing}"
        )
    assert summary_a is not None and summary_b is not None

    a_tx = _index_transcripts(summary_a)
    b_tx = _index_transcripts(summary_b)
    all_sids = sorted(set(a_tx.keys()) | set(b_tx.keys()))
    per_transcript_diff: list[dict[str, Any]] = []
    for sid in all_sids:
        a = a_tx.get(sid)
        b = b_tx.get(sid)
        att_a = int((a or {}).get("chunks_attempted", 0) or 0)
        att_b = int((b or {}).get("chunks_attempted", 0) or 0)
        bl_a = int((a or {}).get("chunks_blocked", 0) or 0)
        bl_b = int((b or {}).get("chunks_blocked", 0) or 0)
        st_a = _stage_status(a)
        st_b = _stage_status(b)
        per_transcript_diff.append(
            {
                "source_id": sid,
                "chunks_attempted_delta": att_b - att_a,
                "chunks_blocked_delta": bl_b - bl_a,
                "stage_status_a": st_a,
                "stage_status_b": st_b,
                "stage_improved": _stage_improved(st_a, st_b),
                "present_in_a": a is not None,
                "present_in_b": b is not None,
            }
        )

    totals_a = summary_a.get("totals") or {}
    totals_b = summary_b.get("totals") or {}
    blocked_rate_a = float(totals_a.get("blocked_rate", 0.0) or 0.0)
    blocked_rate_b = float(totals_b.get("blocked_rate", 0.0) or 0.0)
    totals_diff = {
        "chunks_attempted_delta": int(
            (totals_b.get("chunks_attempted") or 0)
            - (totals_a.get("chunks_attempted") or 0)
        ),
        "chunks_blocked_delta": int(
            (totals_b.get("chunks_blocked") or 0)
            - (totals_a.get("chunks_blocked") or 0)
        ),
        "blocked_rate_a": blocked_rate_a,
        "blocked_rate_b": blocked_rate_b,
        "blocked_rate_delta": blocked_rate_b - blocked_rate_a,
    }

    breakdown_a = summary_a.get("block_reason_breakdown") or {}
    breakdown_b = summary_b.get("block_reason_breakdown") or {}
    all_reasons = sorted(set(breakdown_a.keys()) | set(breakdown_b.keys()))
    block_reason_diff: dict[str, dict[str, int]] = {}
    for reason in all_reasons:
        a = int(breakdown_a.get(reason, 0) or 0)
        b = int(breakdown_b.get(reason, 0) or 0)
        block_reason_diff[reason] = {"run_a": a, "run_b": b, "delta": b - a}

    findings_a = set(_findings_set(summary_a))
    findings_b = set(_findings_set(summary_b))
    new_findings = sorted(findings_b - findings_a)
    resolved_findings = sorted(findings_a - findings_b)

    cov_a, prec_a = _eval_metrics(summary_a)
    cov_b, prec_b = _eval_metrics(summary_b)
    eval_diff: dict[str, Any] | None = None
    if cov_a is not None or cov_b is not None or prec_a is not None or prec_b is not None:
        eval_diff = {
            "coverage_a": cov_a,
            "coverage_b": cov_b,
            "coverage_delta": (
                (cov_b - cov_a) if (cov_a is not None and cov_b is not None) else None
            ),
            "precision_a": prec_a,
            "precision_b": prec_b,
            "precision_delta": (
                (prec_b - prec_a)
                if (prec_a is not None and prec_b is not None)
                else None
            ),
        }

    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "run_a_id": run_a_id,
        "run_b_id": run_b_id,
        "run_a_timestamp": summary_a.get("created_at"),
        "run_b_timestamp": summary_b.get("created_at"),
        "per_transcript_diff": per_transcript_diff,
        "totals_diff": totals_diff,
        "block_reason_diff": block_reason_diff,
        "new_findings_in_b": new_findings,
        "resolved_findings_in_b": resolved_findings,
        "eval_diff": eval_diff,
    }


_UP = "▲"
_DOWN = "▼"
_FLAT = "•"


def _direction(delta: float) -> str:
    if delta > 0:
        return _UP
    if delta < 0:
        return _DOWN
    return _FLAT


def _truncate(value: str, limit: int = _MARKDOWN_SOURCE_ID_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def render_markdown(diff: dict[str, Any]) -> str:
    """Render a markdown report for ``$GITHUB_STEP_SUMMARY``."""
    lines: list[str] = []
    lines.append(
        f"## Pipeline Run Diff: {diff.get('run_a_id')} → {diff.get('run_b_id')}"
    )
    lines.append("")
    lines.append("### Overall")
    totals = diff.get("totals_diff") or {}
    attempted_delta = totals.get("chunks_attempted_delta", 0)
    blocked_delta = totals.get("chunks_blocked_delta", 0)
    rate_delta = totals.get("blocked_rate_delta", 0.0)
    lines.append(
        f"- Chunks attempted: {_direction(attempted_delta)} {attempted_delta:+d}"
    )
    lines.append(
        f"- Chunks blocked: {_direction(-blocked_delta)} {blocked_delta:+d} "
        f"(rate {totals.get('blocked_rate_a', 0.0):.3f} → "
        f"{totals.get('blocked_rate_b', 0.0):.3f})"
    )
    eval_diff = diff.get("eval_diff")
    if isinstance(eval_diff, dict):
        cov_a = eval_diff.get("coverage_a")
        cov_b = eval_diff.get("coverage_b")
        if cov_a is not None and cov_b is not None:
            d = cov_b - cov_a
            lines.append(
                f"- Coverage: {cov_a:.3f} → {cov_b:.3f} ({_direction(d)} {d:+.3f})"
            )
        prec_a = eval_diff.get("precision_a")
        prec_b = eval_diff.get("precision_b")
        if prec_a is not None and prec_b is not None:
            d = prec_b - prec_a
            lines.append(
                f"- Precision: {prec_a:.3f} → {prec_b:.3f} ({_direction(d)} {d:+.3f})"
            )

    per_tx = diff.get("per_transcript_diff") or []
    if per_tx:
        lines.append("")
        lines.append("### Per-transcript changes")
        lines.append(
            "| Transcript | Blocked (A) | Blocked (B) | Δ | Stage (A) | Stage (B) |"
        )
        lines.append("|---|---|---|---|---|---|")
        for row in per_tx:
            sid = _truncate(row.get("source_id") or "")
            blocked_a = int(row.get("present_in_a")) and "" or ""
            present_a = bool(row.get("present_in_a"))
            present_b = bool(row.get("present_in_b"))
            blocked_delta_row = int(row.get("chunks_blocked_delta", 0))
            if not present_a and present_b:
                blocked_a_disp = "—"
            else:
                # For display we need the actual blocked counts; we
                # only carry deltas + stage in the diff. Show the
                # delta in column Δ; A/B columns are derived from
                # stage status presence for legibility.
                blocked_a_disp = ""
            stage_a = row.get("stage_status_a") or "—"
            stage_b = row.get("stage_status_b") or "—"
            lines.append(
                f"| {sid} | {'—' if not present_a else ''} | "
                f"{'—' if not present_b else ''} | "
                f"{blocked_delta_row:+d} | {stage_a} | {stage_b} |"
            )

    resolved = diff.get("resolved_findings_in_b") or []
    if resolved:
        lines.append("")
        lines.append("### Findings resolved ✅")
        for f in resolved:
            lines.append(f"- {f}")

    new_f = diff.get("new_findings_in_b") or []
    if new_f:
        lines.append("")
        lines.append("### New findings ⚠️")
        for f in new_f:
            lines.append(f"- {f}")

    return "\n".join(lines) + "\n"


def write_diff_artifact(
    diff: dict[str, Any],
    *,
    data_lake_path: str | Path,
) -> Path | None:
    target_dir = (
        Path(data_lake_path)
        / "store"
        / "artifacts"
        / "pipeline_run_diffs"
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("pipeline_run_diff_mkdir_failed: %s", exc)
        return None
    target = target_dir / f"{diff['run_a_id']}__vs__{diff['run_b_id']}.json"
    try:
        target.write_text(
            json.dumps(diff, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("pipeline_run_diff_write_failed: %s", exc)
        return None

    try:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )
        try:
            validate_artifact(diff, ARTIFACT_TYPE)
        except (ArtifactValidationError, SchemaNotFoundError) as exc:
            _LOG.warning("pipeline_run_diff_schema_violation: %s", exc)
    except ImportError:
        pass
    return target


def _emit_missing_finding(
    *,
    data_lake_path: str | Path,
    run_id: str,
    pipeline_run_id: str | None,
) -> None:
    try:
        write_finding(
            HealthFinding(
                finding_code="pipeline_run_summary_missing",
                severity="info",
                pipeline_run_id=pipeline_run_id,
                context={"missing_run_id": run_id},
                remediation=(
                    "The pipeline_run_summary artifact for this run was "
                    "not found at "
                    "<data_lake>/store/artifacts/pipeline_runs/"
                    f"{run_id}.json. Re-run the pipeline summary step or "
                    "supply the correct run id."
                ),
            ),
            data_lake_path=data_lake_path,
        )
    except Exception as exc:  # never propagate
        _LOG.warning("pipeline_run_summary_missing_write_failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectrum_systems_core.health.run_diff",
        description=(
            "Diff two pipeline_run_summary artifacts and emit a "
            "pipeline_run_diff artifact + markdown report."
        ),
    )
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--run-a-id", required=True)
    parser.add_argument("--run-b-id", required=True)
    parser.add_argument(
        "--output-format",
        choices=("github_actions_summary", "markdown", "json"),
        default="github_actions_summary",
    )
    args = parser.parse_args(argv)

    try:
        diff = diff_runs(args.run_a_id, args.run_b_id, args.data_lake)
    except PipelineRunSummaryMissing as exc:
        msg = str(exc)
        sys.stderr.write(f"ERROR: {msg}\n")
        # Emit one finding per missing run id so the diff workflow
        # rejects the request with an actionable artifact.
        for sid in (args.run_a_id, args.run_b_id):
            if not _summary_path(args.data_lake, sid).is_file():
                _emit_missing_finding(
                    data_lake_path=args.data_lake,
                    run_id=sid,
                    pipeline_run_id=None,
                )
        return 1

    write_diff_artifact(diff, data_lake_path=args.data_lake)
    if args.output_format == "json":
        print(json.dumps(diff, indent=2, sort_keys=True))
        return 0
    md = render_markdown(diff)
    if args.output_format == "github_actions_summary":
        emit_step_summary(md)
        return 0
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
