"""CostTrendReporter — 30-day window cost comparison.

FINDING-I-005: trend windows are deterministic. If history < 60 days
total, report status="insufficient_history" — never produce misleading
aggregates.
"""
from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ..harness.run_history import RunHistoryStore
from . import COST_TREND_WINDOW_DAYS
from ._io import find_prior_audit, parse_iso, utcnow_iso, write_audit_record
from ._schema import validate_governance_artifact


_LOG = logging.getLogger(__name__)


def _timestamp_for(run: Dict[str, Any]) -> datetime.datetime | None:
    for key in ("completed_at", "started_at", "recorded_at"):
        ts = parse_iso(run.get(key))
        if ts is not None:
            return ts
    return None


def _trend_status(delta_pct: float | None) -> tuple[str, str | None]:
    if delta_pct is None:
        return "stable", None
    if delta_pct > 25:
        return "degrading", "high"
    if 10 < delta_pct <= 25:
        return "degrading", "medium"
    if -10 <= delta_pct <= 10:
        return "stable", None
    return "improving", None


class CostTrendReporter:
    """30-day window comparison reporter."""

    def scan(self, repo_root: str | Path) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: List[Dict[str, Any]] = []
        drift_signals: List[Dict[str, Any]] = []

        runs = RunHistoryStore().get_recent_runs(
            repo_root_path, n=10_000
        )
        timed: List[tuple[datetime.datetime, Dict[str, Any]]] = []
        for run in runs:
            ts = _timestamp_for(run)
            if ts is not None:
                timed.append((ts, run))

        now = datetime.datetime.now(datetime.timezone.utc)
        window_size = datetime.timedelta(days=COST_TREND_WINDOW_DAYS)
        current_cutoff = now - window_size
        prior_cutoff = now - 2 * window_size

        if not timed:
            history_span_days = 0
        else:
            earliest = min(ts for ts, _ in timed)
            history_span_days = (now - earliest).days

        prior_audit = find_prior_audit(repo_root_path, "cost_trend")
        prior_value = prior_audit.get("current_value") if prior_audit else None

        if history_span_days < 2 * COST_TREND_WINDOW_DAYS:
            current_value: Dict[str, Any] = {
                "status": "insufficient_history",
                "current_30d_total": None,
                "prior_30d_total": None,
                "delta_pct": None,
                "history_span_days": history_span_days,
            }
            record = {
                "audit_id": str(uuid.uuid4()),
                "audit_type": "cost_trend",
                "scope": "system_wide",
                "generated_at": utcnow_iso(),
                "current_value": current_value,
                "prior_value": prior_value,
                "delta": None,
                "flagged_items": [],
                "total_scanned": len(runs),
                "total_flagged": 0,
                "status": "insufficient_history",
            }
            record["_drift_signals"] = []
            ok, err = validate_governance_artifact(
                {k: v for k, v in record.items() if not k.startswith("_")},
                "governance_audit_record",
            )
            if not ok:
                _LOG.warning("cost_trend audit failed validation: %s", err)
                record["status"] = "error"
            persisted = {k: v for k, v in record.items() if not k.startswith("_")}
            write_audit_record(persisted, repo_root_path)
            return record

        current_total = sum(
            float(run.get("total_cost_usd") or 0.0)
            for ts, run in timed
            if ts >= current_cutoff
        )
        prior_total = sum(
            float(run.get("total_cost_usd") or 0.0)
            for ts, run in timed
            if prior_cutoff <= ts < current_cutoff
        )
        delta_pct: float | None
        if prior_total > 0:
            delta_pct = ((current_total - prior_total) / prior_total) * 100.0
        else:
            delta_pct = None

        status_label, severity = _trend_status(delta_pct)
        if severity:
            detail = (
                f"Cost trend (30d vs prior 30d): {status_label} "
                f"(delta {delta_pct:.2f}%). current=${current_total:.4f} "
                f"prior=${prior_total:.4f}"
            )
            flagged.append(
                {
                    "item_type": "cost_trend",
                    "item_id": "cost_trend_30d",
                    "detail": detail,
                    "severity": severity,
                    "recommended_action": (
                        "Investigate sources of cost growth — review the "
                        "highest-cost recent runs"
                    ),
                }
            )
            drift_signals.append(
                {
                    "signal_id": str(uuid.uuid4()),
                    "signal_type": "cost_increase",
                    "signal_strength": (
                        "strong" if severity == "high" else "moderate"
                    ),
                    "affected_artifact_types": ["run_history_entry"],
                    "detail": detail,
                    "baseline_value": float(prior_total),
                    "current_value": float(current_total),
                    "delta_pct": float(delta_pct) if delta_pct is not None else None,
                    "detected_at": utcnow_iso(),
                }
            )

        current_value = {
            "status": status_label,
            "current_30d_total": float(current_total),
            "prior_30d_total": float(prior_total),
            "delta_pct": float(delta_pct) if delta_pct is not None else None,
            "history_span_days": history_span_days,
        }
        delta = None
        if prior_value is not None and prior_value.get("current_30d_total") is not None:
            delta = {
                "current_30d_total": float(current_total)
                - float(prior_value.get("current_30d_total") or 0.0),
            }

        overall_status = "drift_detected" if flagged else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "cost_trend",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": len(runs),
            "total_flagged": len(flagged),
            "status": overall_status,
        }
        record["_drift_signals"] = drift_signals
        ok, err = validate_governance_artifact(
            {k: v for k, v in record.items() if not k.startswith("_")},
            "governance_audit_record",
        )
        if not ok:
            _LOG.warning("cost_trend audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
            record["_drift_signals"] = []
        persisted = {k: v for k, v in record.items() if not k.startswith("_")}
        write_audit_record(persisted, repo_root_path)
        return record
