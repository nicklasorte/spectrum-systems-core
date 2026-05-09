"""GovernanceDashboard — runs all scanners and composes a 30-line summary.

FINDING-I-007: dashboard.md is capped at DASHBOARD_SUMMARY_MAX_LINES = 30.
Detail goes to linked governance/markdown/audit_<audit_id>.md files.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

from . import DASHBOARD_SUMMARY_MAX_LINES
from ._io import (
    read_json,
    utcnow_iso,
    write_json,
)
from ._paths import (
    audits_dir,
    dashboard_latest_path,
    ensure_governance_tree,
    markdown_dir,
)
from ._schema import validate_governance_artifact
from .compression_scanner import CompressionScanner
from .cost_trend_reporter import CostTrendReporter
from .decision_divergence_detector import DecisionDivergenceDetector
from .eval_coverage_scanner import EvalCoverageScanner
from .exception_accumulation_tracker import ExceptionAccumulationTracker
from .hidden_logic_scanner import HiddenLogicScanner
from .markdown_authority_scanner import MarkdownAuthorityScanner
from .schema_drift_scanner import SchemaDriftScanner


_LOG = logging.getLogger(__name__)


_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_STRENGTH_ORDER = {"strong": 0, "moderate": 1, "weak": 2}


def _pick_top_drift_signals(
    pools: List[List[Dict[str, Any]]],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    combined: List[Dict[str, Any]] = []
    for pool in pools:
        combined.extend(pool or [])
    combined.sort(
        key=lambda s: (
            _STRENGTH_ORDER.get(s.get("signal_strength", "weak"), 99),
            -1 * (s.get("current_value") or 0.0),
            s.get("detected_at", ""),
        )
    )
    return combined[:limit]


def _pick_top_candidates(
    pool: List[Dict[str, Any]], limit: int = 5
) -> List[Dict[str, Any]]:
    pool = list(pool or [])
    pool.sort(
        key=lambda c: (
            c.get("candidate_type", ""),
            c.get("candidate_path", ""),
        )
    )
    return pool[:limit]


def _format_dashboard_md(
    dashboard: Dict[str, Any],
    audit_id: str,
) -> str:
    schema_health = dashboard["schema_health"]
    eval_health = dashboard["eval_health"]
    decision = dashboard["decision_consistency"]
    cost = dashboard["cost_trend"]
    drift_signals = dashboard.get("drift_signals", [])[:5]
    top_candidates = dashboard.get("top_candidates", [])[:5]

    cost_delta = cost.get("delta_pct")
    cost_delta_s = (
        f"{cost_delta:+.1f}%" if isinstance(cost_delta, (int, float)) else "n/a"
    )
    divergence_pct = (decision.get("divergence_rate") or 0.0) * 100.0

    lines: List[str] = [
        "# Governance Dashboard",
        f"Generated: {dashboard['generated_at']}  Audit: {audit_id}",
        "> VIEW ONLY. Regenerated on every audit. Do not edit.",
        "## Health summary",
        (
            f"Schema health: total={schema_health['total_schemas']} "
            f"no_evals={schema_health['schemas_with_no_evals']} "
            f"unused_60d={schema_health['schemas_unused_60d']}"
        ),
        (
            f"Eval health: total={eval_health['total_evals']} "
            f"no_recent_failures={eval_health['evals_no_recent_failures']} "
            f"degrading={eval_health['evals_degrading']}"
        ),
        (
            f"Decision consistency: "
            f"runs={decision['total_runs_analyzed']} "
            f"divergent={decision['divergent_outcome_count']} "
            f"({divergence_pct:.1f}%)"
        ),
        (
            f"Cost trend (30d vs prior 30d): {cost.get('status', '?')} "
            f"({cost_delta_s})"
        ),
        f"Hidden logic creep: {dashboard.get('hidden_logic_total', 0)} findings",
        "## Drift signals",
    ]
    if not drift_signals:
        lines.append("- (none)")
    else:
        for sig in drift_signals:
            lines.append(
                f"- [{sig.get('signal_strength', '?')}] "
                f"{sig.get('signal_type', '?')}: "
                f"{(sig.get('detail') or '')[:90]}"
            )
    lines.append("## Top compression candidates")
    if not top_candidates:
        lines.append("- (none)")
    else:
        for cand in top_candidates:
            lines.append(
                f"- {cand.get('candidate_type', '?')}: "
                f"{cand.get('candidate_name', '?')} — "
                f"{cand.get('recommended_action', '?')}"
            )
    lines.append("---")
    lines.append(f"Last audit: {dashboard['last_audit_timestamp']}")
    lines.append(f"Detail: governance/markdown/audit_{audit_id}.md")
    return "\n".join(lines) + "\n"


def _format_audit_detail_md(
    audit_id: str,
    generated_at: str,
    sections: List[Dict[str, Any]],
) -> str:
    lines: List[str] = [
        f"# Governance Audit Detail — {audit_id}",
        f"Generated: {generated_at}",
        "",
        "> VIEW ONLY. Detail file. Regenerated on each audit run.",
        "",
    ]
    for section in sections:
        title = section.get("title", "")
        record = section.get("record", {})
        flagged = record.get("flagged_items") or []
        lines.append(f"## {title}")
        lines.append(
            f"- audit_id: `{record.get('audit_id', '')}`  "
            f"status: {record.get('status', '?')}  "
            f"flagged: {record.get('total_flagged', 0)}"
        )
        if not flagged:
            lines.append("- (no findings)")
        else:
            for item in flagged[:50]:
                lines.append(
                    f"- [{item.get('severity', '?')}] "
                    f"{item.get('item_type', '?')} "
                    f"`{item.get('item_id', '')}`: "
                    f"{item.get('detail', '')}"
                )
                lines.append(
                    f"  - action: {item.get('recommended_action', '')}"
                )
        lines.append("")
    return "\n".join(lines) + "\n"


class GovernanceDashboard:
    """Run all scanners + write summary projection (capped at 30 lines)."""

    def generate(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        ensure_governance_tree(repo_root_path)

        schema_record = SchemaDriftScanner().scan(repo_root_path)
        eval_record = EvalCoverageScanner().scan(repo_root_path)
        divergence_record = DecisionDivergenceDetector().scan(repo_root_path)
        exception_record = ExceptionAccumulationTracker().scan(repo_root_path)
        hidden_record = HiddenLogicScanner().scan(repo_root_path)
        markdown_record = MarkdownAuthorityScanner().scan(repo_root_path)
        cost_record = CostTrendReporter().scan(repo_root_path)
        compression_record = CompressionScanner().scan(repo_root_path)

        all_records = [
            schema_record,
            eval_record,
            divergence_record,
            exception_record,
            hidden_record,
            markdown_record,
            cost_record,
            compression_record,
        ]
        total_flagged = sum(int(r.get("total_flagged") or 0) for r in all_records)
        high_count = sum(
            sum(1 for f in r.get("flagged_items") or [] if f.get("severity") == "high")
            for r in all_records
        )

        audit_id = str(uuid.uuid4())
        generated_at = utcnow_iso()

        # Schema health
        schema_current = schema_record.get("current_value") or {}
        eval_current = eval_record.get("current_value") or {}
        divergence_current = divergence_record.get("current_value") or {}
        cost_current = cost_record.get("current_value") or {}

        # schemas_unused_60d: count unique schema candidates
        compression_candidates = compression_record.get("_candidates") or []
        unused_schemas_60d = sum(
            1
            for c in compression_candidates
            if c.get("candidate_type") == "schema"
        )

        schema_health = {
            "total_schemas": int(schema_current.get("total_schemas") or 0),
            "schemas_with_no_evals": sum(
                1
                for f in schema_record.get("flagged_items") or []
                if f.get("item_type") == "schema_without_eval"
            ),
            "schemas_unused_60d": unused_schemas_60d,
        }
        eval_health = {
            "total_evals": int(eval_current.get("total_evals") or 0),
            "evals_no_recent_failures": int(
                eval_current.get("never_failing") or 0
            ),
            "evals_degrading": int(eval_current.get("degrading") or 0),
        }
        decision_consistency = {
            "total_runs_analyzed": int(
                divergence_current.get("total_runs_analyzed") or 0
            ),
            "divergent_outcome_count": int(
                divergence_current.get("divergent_groups") or 0
            ),
            "divergence_rate": float(
                divergence_current.get("divergence_rate") or 0.0
            ),
        }
        cost_trend = {
            "status": str(cost_current.get("status") or "insufficient_history"),
            "current_30d_total": (
                float(cost_current["current_30d_total"])
                if cost_current.get("current_30d_total") is not None
                else None
            ),
            "prior_30d_total": (
                float(cost_current["prior_30d_total"])
                if cost_current.get("prior_30d_total") is not None
                else None
            ),
            "delta_pct": (
                float(cost_current["delta_pct"])
                if cost_current.get("delta_pct") is not None
                else None
            ),
        }

        drift_signals = _pick_top_drift_signals(
            [
                divergence_record.get("_drift_signals") or [],
                exception_record.get("_drift_signals") or [],
                cost_record.get("_drift_signals") or [],
            ],
            limit=5,
        )
        top_candidates = _pick_top_candidates(compression_candidates, limit=5)

        dashboard: Dict[str, Any] = {
            "generated_at": generated_at,
            "audit_id": audit_id,
            "schema_health": schema_health,
            "eval_health": eval_health,
            "decision_consistency": decision_consistency,
            "cost_trend": cost_trend,
            "drift_signals": drift_signals,
            "top_candidates": top_candidates,
            "last_audit_timestamp": generated_at,
        }
        ok, err = validate_governance_artifact(
            dashboard, "governance_dashboard"
        )
        if not ok:
            _LOG.warning("governance_dashboard failed validation: %s", err)
            return {
                "status": "failure",
                "audit_id": audit_id,
                "total_flagged": total_flagged,
                "high_count": high_count,
                "reason": f"schema_violation: {err}",
            }

        write_json(dashboard_latest_path(repo_root_path), dashboard)

        dashboard_md_dashboard = {
            **dashboard,
            "hidden_logic_total": int(hidden_record.get("total_flagged") or 0),
        }
        dashboard_md_text = _format_dashboard_md(dashboard_md_dashboard, audit_id)
        # Enforce line cap for the summary (FINDING-I-007).
        md_lines = dashboard_md_text.rstrip("\n").split("\n")
        if len(md_lines) > DASHBOARD_SUMMARY_MAX_LINES:
            md_lines = md_lines[:DASHBOARD_SUMMARY_MAX_LINES]
            dashboard_md_text = "\n".join(md_lines) + "\n"
        dashboard_md_path = markdown_dir(repo_root_path) / "dashboard.md"
        dashboard_md_path.write_text(dashboard_md_text, encoding="utf-8")

        # Detail file: full per-section breakdown.
        detail_md_text = _format_audit_detail_md(
            audit_id,
            generated_at,
            sections=[
                {"title": "Schema drift", "record": schema_record},
                {"title": "Eval coverage", "record": eval_record},
                {"title": "Decision divergence", "record": divergence_record},
                {
                    "title": "Exception accumulation",
                    "record": exception_record,
                },
                {"title": "Hidden logic creep", "record": hidden_record},
                {"title": "Markdown authority", "record": markdown_record},
                {"title": "Cost trend", "record": cost_record},
                {"title": "Compression scan", "record": compression_record},
            ],
        )
        detail_path = markdown_dir(repo_root_path) / f"audit_{audit_id}.md"
        detail_path.write_text(detail_md_text, encoding="utf-8")

        candidates_md = _format_candidates_md(top_candidates, generated_at)
        (markdown_dir(repo_root_path) / "candidates.md").write_text(
            candidates_md, encoding="utf-8"
        )

        if vault_root:
            self._copy_to_vault(repo_root_path, vault_root, audit_id)

        return {
            "status": "success",
            "audit_id": audit_id,
            "total_flagged": total_flagged,
            "high_count": high_count,
            "dashboard": dashboard,
        }

    def _copy_to_vault(
        self,
        repo_root_path: Path,
        vault_root: str | Path,
        audit_id: str,
    ) -> None:
        """Copy dashboard.md and detail file to vault/Governance/."""
        from ..ingestion.obsidian_projection import ObsidianProjection

        ObsidianProjection().write_governance_dashboard_projection(
            repo_root_path, vault_root, audit_id
        )


def _format_candidates_md(
    candidates: List[Dict[str, Any]], generated_at: str
) -> str:
    lines: List[str] = [
        "# Governance — Compression Candidates",
        f"Generated: {generated_at}",
        "> VIEW ONLY. Recommendations only. apply-compression CLI to act.",
        "",
    ]
    if not candidates:
        lines.append("_(no proposed candidates)_")
        return "\n".join(lines) + "\n"
    for cand in candidates:
        lines.append(
            f"- **{cand.get('candidate_type', '?')}** "
            f"`{cand.get('candidate_path', '')}` "
            f"name=`{cand.get('candidate_name', '')}` "
            f"action=`{cand.get('recommended_action', '?')}`"
        )
        lines.append(f"    - reason: {cand.get('reason', '')}")
    lines.append("")
    return "\n".join(lines) + "\n"
