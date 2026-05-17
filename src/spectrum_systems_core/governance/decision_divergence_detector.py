"""DecisionDivergenceDetector — same input class -> different outcomes.

FINDING-I-002: input class is the deterministic tuple
(task_type, recipe_id, audience). Runs missing any of these are excluded
and logged to governance/drift/skipped_runs.jsonl. Never crashes on
missing fields.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ..harness.run_history import RunHistoryStore
from . import DIVERGENCE_KEY_FIELDS
from ._io import (
    append_jsonl,
    find_prior_audit,
    utcnow_iso,
    write_audit_record,
)
from ._paths import skipped_runs_path
from ._schema import validate_governance_artifact

_LOG = logging.getLogger(__name__)


def _key_for_run(run: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return the (task_type, recipe_id, audience) tuple, or None if any missing."""
    values: list[str] = []
    for field in DIVERGENCE_KEY_FIELDS:
        if field == "task_type":
            value = (
                run.get("task_type")
                or run.get("purpose")
                or run.get("run_type")
            )
        elif field == "recipe_id":
            value = run.get("recipe_id")
        elif field == "audience":
            value = run.get("audience")
        else:
            value = run.get(field)
        if not isinstance(value, str) or not value:
            return None
        values.append(value)
    return tuple(values)  # type: ignore[return-value]


def _severity_for_outcomes(outcomes: list[str]) -> str:
    distinct = set(outcomes)
    if "success" in distinct and "blocked" in distinct:
        return "high"
    if "success" in distinct and "partial" in distinct:
        return "medium"
    return "low"


class DecisionDivergenceDetector:
    """Detect runs that share an input-class but produced different outcomes."""

    def scan(self, repo_root: str | Path) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: list[dict[str, Any]] = []
        drift_signals: list[dict[str, Any]] = []

        runs = RunHistoryStore().get_recent_runs(repo_root_path, n=1000)
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        skipped_count = 0

        for run in runs:
            key = _key_for_run(run)
            if key is None:
                skipped_count += 1
                try:
                    append_jsonl(
                        skipped_runs_path(repo_root_path),
                        {
                            "entry_id": run.get("entry_id"),
                            "run_id": run.get("run_id"),
                            "skipped_at": utcnow_iso(),
                            "reason": "missing_required_key_field",
                            "missing_fields": [
                                f
                                for f in DIVERGENCE_KEY_FIELDS
                                if not run.get(f)
                                and f != "task_type"
                            ],
                        },
                    )
                except Exception as exc:  # pragma: no cover
                    _LOG.warning("skipped_runs.jsonl write failed: %s", exc)
                continue
            groups.setdefault(key, []).append(run)

        divergent_groups = 0
        for key, group_runs in groups.items():
            if len(group_runs) < 2:
                continue
            outcomes = [r.get("outcome") or "unknown" for r in group_runs]
            distinct = set(outcomes)
            if len(distinct) <= 1:
                continue
            divergent_groups += 1
            severity = _severity_for_outcomes(outcomes)
            detail = (
                f"Key {list(key)} produced {len(group_runs)} runs with "
                f"outcomes {sorted(distinct)}"
            )
            flagged.append(
                {
                    "item_type": "decision_divergence",
                    "item_id": "::".join(key),
                    "detail": detail,
                    "severity": severity,
                    "recommended_action": (
                        "Investigate divergent outcomes for key "
                        + "::".join(key)
                    ),
                }
            )
            strength = (
                "strong"
                if severity == "high"
                else "moderate"
                if severity == "medium"
                else "weak"
            )
            drift_signals.append(
                {
                    "signal_id": str(uuid.uuid4()),
                    "signal_type": "decision_divergence",
                    "signal_strength": strength,
                    "affected_artifact_types": ["run_history_entry"],
                    "detail": detail,
                    "baseline_value": None,
                    "current_value": float(len(distinct)),
                    "delta_pct": None,
                    "detected_at": utcnow_iso(),
                }
            )

        total_groups = len(groups)
        divergence_rate = (
            (divergent_groups / total_groups) if total_groups else 0.0
        )

        prior_audit = find_prior_audit(repo_root_path, "decision_divergence")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: dict[str, Any] = {
            "total_runs_analyzed": len(runs) - skipped_count,
            "total_groups": total_groups,
            "divergent_groups": divergent_groups,
            "divergence_rate": divergence_rate,
            "skipped_runs": skipped_count,
        }
        delta: dict[str, Any] | None = None
        if prior_value is not None:
            delta = {
                "divergent_groups": int(divergent_groups)
                - int(prior_value.get("divergent_groups", 0)),
                "divergence_rate": float(divergence_rate)
                - float(prior_value.get("divergence_rate", 0.0)),
            }

        status = "drift_detected" if flagged else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "decision_divergence",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": len(runs),
            "total_flagged": len(flagged),
            "status": status,
        }
        record["_drift_signals"] = drift_signals  # internal, stripped before write
        ok, err = validate_governance_artifact(
            {k: v for k, v in record.items() if not k.startswith("_")},
            "governance_audit_record",
        )
        if not ok:
            _LOG.warning("decision_divergence audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
            record["_drift_signals"] = []
        persisted = {k: v for k, v in record.items() if not k.startswith("_")}
        write_audit_record(persisted, repo_root_path)
        return record
