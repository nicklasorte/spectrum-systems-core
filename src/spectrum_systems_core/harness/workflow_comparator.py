"""WorkflowComparator — compare two synthesis runs across fixed dimensions.

FINDING-G-005: vault projection makes the comparison visible in Obsidian.
The canonical JSON artifact is always written to harness/comparisons/.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ._io import parse_iso, read_json, utcnow_iso, write_json
from ._paths import comparisons_dir, runs_index_path
from ._schema import validate_harness_artifact


_LOG = logging.getLogger(__name__)


# (dimension_name, lower_is_better)
COMPARISON_DIMENSIONS: List[tuple[str, bool]] = [
    ("total_cost_usd", True),
    ("eval_pass_count", False),
    ("eval_fail_count", True),
    ("eval_warn_count", True),
    ("grounded_section_count", False),
    ("ungrounded_section_count", True),
    ("keynote_arc_beat_count", False),
    ("run_duration_seconds", True),
]


def _load_run_view(run_id: str, repo_root: Path) -> Dict[str, Any] | None:
    """Build a comparison view from synthesis/<run_id>/."""
    run_dir = repo_root / "synthesis" / run_id
    manifest = read_json(run_dir / "run_manifest.json")
    if manifest is None:
        return None
    report_draft = read_json(run_dir / "report_draft.json") or {}
    keynote_scaffold = read_json(run_dir / "keynote_scaffold.json") or {}

    sections = report_draft.get("sections", []) or []
    grounded = sum(1 for s in sections if s.get("grounded"))
    ungrounded = sum(1 for s in sections if not s.get("grounded"))
    arc = keynote_scaffold.get("arc", []) or []

    started = parse_iso(manifest.get("started_at"))
    completed = parse_iso(manifest.get("completed_at"))
    duration: float | None = None
    if started and completed:
        duration = max(0.0, (completed - started).total_seconds())

    return {
        "run_id": run_id,
        "manifest": manifest,
        "values": {
            "total_cost_usd": float(
                manifest.get("total_estimated_cost_usd", 0.0) or 0.0
            ),
            "eval_pass_count": int(grounded),
            "eval_fail_count": int(ungrounded),
            "eval_warn_count": 0,
            "grounded_section_count": int(grounded),
            "ungrounded_section_count": int(ungrounded),
            "keynote_arc_beat_count": int(len(arc)),
            "run_duration_seconds": duration,
        },
    }


def _direction(value_a: Any, value_b: Any, lower_is_better: bool) -> tuple[Any, str]:
    if value_a is None or value_b is None:
        return None, "not_comparable"
    if not isinstance(value_a, (int, float)) or not isinstance(value_b, (int, float)):
        return None, "not_comparable"
    delta = value_b - value_a
    if delta == 0:
        return delta, "unchanged"
    if lower_is_better:
        return delta, "improved" if delta < 0 else "degraded"
    return delta, "improved" if delta > 0 else "degraded"


def _summary_and_action(
    dimensions: List[Dict[str, Any]],
    view_a: Dict[str, Any],
    view_b: Dict[str, Any],
) -> tuple[str, str]:
    cost_dim = next(
        (d for d in dimensions if d["dimension_name"] == "total_cost_usd"),
        None,
    )
    grounded_dim = next(
        (d for d in dimensions if d["dimension_name"] == "grounded_section_count"),
        None,
    )
    cost_phrase = ""
    if cost_dim and cost_dim["direction"] != "not_comparable":
        delta = cost_dim.get("delta") or 0
        try:
            pct = (
                abs(float(delta))
                / max(float(view_a["values"]["total_cost_usd"]), 1e-9)
                * 100.0
            )
        except (TypeError, ValueError):
            pct = 0.0
        cost_phrase = (
            f"Run B was ${abs(float(delta)):.4f} ({pct:.1f}%) "
            + ("cheaper" if float(delta) < 0 else "more expensive")
            + " than Run A."
        )
    grounded_phrase = ""
    if grounded_dim and grounded_dim["direction"] != "not_comparable":
        delta = grounded_dim.get("delta") or 0
        if delta:
            grounded_phrase = (
                f" Run B had {abs(int(delta))} "
                + ("more" if int(delta) > 0 else "fewer")
                + " grounded sections."
            )

    summary = (cost_phrase + grounded_phrase).strip() or (
        "Runs are comparable across all dimensions; no significant deltas."
    )

    improved = sum(1 for d in dimensions if d["direction"] == "improved")
    degraded = sum(1 for d in dimensions if d["direction"] == "degraded")
    if improved > degraded:
        action = (
            "Run B improved on more dimensions; consider its retrieval recipe "
            "as the default for this audience."
        )
    elif degraded > improved:
        action = (
            "Run B degraded on more dimensions; investigate Run A's setup "
            "before adopting changes."
        )
    else:
        action = "No clear winner; review individual dimensions before deciding."
    return summary, action


def _vault_projection(
    comparison: Dict[str, Any],
    vault_root: str | Path,
) -> str:
    target = (
        Path(vault_root).resolve()
        / "Harness"
        / "comparisons"
        / f"{comparison['run_id_a']}_vs_{comparison['run_id_b']}.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        "---",
        f"comparison_id: {comparison['comparison_id']}",
        f"run_id_a: {comparison['run_id_a']}",
        f"run_id_b: {comparison['run_id_b']}",
        f"compared_at: {comparison['compared_at']}",
        "vault_note_status: projection",
        "---",
        "",
        f"# Workflow Comparison — {comparison['run_id_a'][:8]} vs {comparison['run_id_b'][:8]}",
        "",
        f"> Generated: {comparison['compared_at']} | VIEW ONLY",
        "> Regenerated on every comparison. Do not edit.",
        "",
        f"**Summary:** {comparison['summary']}",
        "",
        f"**Recommended action:** {comparison['recommended_action']}",
        "",
        "## Dimensions",
        "",
        "| dimension | run_a | run_b | delta | direction |",
        "| --------- | ----- | ----- | ----- | --------- |",
    ]
    for dim in comparison["dimensions"]:
        lines.append(
            f"| {dim['dimension_name']} | {dim['value_a']} | "
            f"{dim['value_b']} | {dim['delta']} | {dim['direction']} |"
        )
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return str(target)


class WorkflowComparator:
    def compare(
        self,
        run_id_a: str,
        run_id_b: str,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> Dict[str, Any]:
        try:
            if not run_id_a or not run_id_b:
                return {
                    "status": "failure",
                    "comparison_id": "",
                    "reason": "missing_run_ids",
                }
            if run_id_a == run_id_b:
                return {
                    "status": "failure",
                    "comparison_id": "",
                    "reason": "cannot compare a run to the same run",
                }

            repo_root_path = Path(repo_root).resolve()
            index = read_json(runs_index_path(repo_root_path)) or {"runs": []}
            known_ids = {e.get("run_id") for e in index.get("runs", [])}

            for rid in (run_id_a, run_id_b):
                if rid not in known_ids:
                    return {
                        "status": "failure",
                        "comparison_id": "",
                        "reason": f"run not found in run history index: {rid}",
                    }

            view_a = _load_run_view(run_id_a, repo_root_path)
            view_b = _load_run_view(run_id_b, repo_root_path)
            if view_a is None:
                return {
                    "status": "failure",
                    "comparison_id": "",
                    "reason": f"run manifest not found: {run_id_a}",
                }
            if view_b is None:
                return {
                    "status": "failure",
                    "comparison_id": "",
                    "reason": f"run manifest not found: {run_id_b}",
                }

            dimensions: List[Dict[str, Any]] = []
            for name, lower_is_better in COMPARISON_DIMENSIONS:
                a_val = view_a["values"].get(name)
                b_val = view_b["values"].get(name)
                delta, direction = _direction(a_val, b_val, lower_is_better)
                dimensions.append(
                    {
                        "dimension_name": name,
                        "value_a": a_val,
                        "value_b": b_val,
                        "delta": delta,
                        "direction": direction,
                    }
                )

            summary, action = _summary_and_action(dimensions, view_a, view_b)
            comparison: Dict[str, Any] = {
                "comparison_id": str(uuid.uuid4()),
                "run_id_a": run_id_a,
                "run_id_b": run_id_b,
                "compared_at": utcnow_iso(),
                "dimensions": dimensions,
                "summary": summary,
                "recommended_action": action,
                "vault_projection_path": None,
            }

            ok, err = validate_harness_artifact(comparison, "workflow_comparison")
            if not ok:
                return {
                    "status": "failure",
                    "comparison_id": "",
                    "reason": f"schema_violation: {err}",
                }

            json_target = (
                comparisons_dir(repo_root_path)
                / f"{run_id_a}_vs_{run_id_b}.json"
            )
            if vault_root:
                comparison["vault_projection_path"] = _vault_projection(
                    comparison, vault_root
                )
            write_json(json_target, comparison)

            return {
                "status": "success",
                "comparison_id": comparison["comparison_id"],
                "json_path": str(json_target),
                "vault_projection_path": comparison["vault_projection_path"],
                "summary": summary,
                "recommended_action": action,
                "reason": "",
            }
        except Exception as exc:  # pragma: no cover
            _LOG.warning("WorkflowComparator.compare failed: %s", exc)
            return {
                "status": "failure",
                "comparison_id": "",
                "reason": f"unexpected_error: {exc}",
            }
