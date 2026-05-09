"""ExceptionAccumulationTracker — overrides that should become policy.

Reuses OverrideStore. Groups overrides by keyword (first 5 significant
words of decision_context) plus overridden_eval_or_block. Threshold is
EXCEPTION_ACCUMULATION_THRESHOLD = 5.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ..harness.override_store import OverrideStore
from ..harness._io import read_json
from ..harness._paths import overrides_archive_dir
from . import EXCEPTION_ACCUMULATION_THRESHOLD
from ._io import find_prior_audit, utcnow_iso, write_audit_record
from ._schema import validate_governance_artifact


_LOG = logging.getLogger(__name__)

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
    "is",
    "with",
    "in",
    "on",
    "at",
    "by",
    "from",
    "this",
    "that",
}


def _keyword(decision_context: str, num_words: int = 5) -> str:
    if not isinstance(decision_context, str):
        return ""
    cleaned = decision_context.lower()
    words = [w for w in cleaned.split() if w and w not in _STOPWORDS]
    return " ".join(words[:num_words])


def _load_archived_overrides(repo_root: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    archive = overrides_archive_dir(repo_root)
    if not archive.is_dir():
        return out
    for path in sorted(archive.glob("*.json")):
        record = read_json(path)
        if isinstance(record, dict):
            out.append(record)
    return out


class ExceptionAccumulationTracker:
    """Find override patterns to promote to policy."""

    def scan(self, repo_root: str | Path) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: List[Dict[str, Any]] = []
        drift_signals: List[Dict[str, Any]] = []

        actives = OverrideStore().get_active_overrides(repo_root_path)
        archived = _load_archived_overrides(repo_root_path)
        all_overrides: List[Dict[str, Any]] = list(actives) + list(archived)

        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for override in all_overrides:
            keyword = _keyword(str(override.get("decision_context", "")))
            eval_id = str(override.get("overridden_eval_or_block", ""))
            key = (keyword, eval_id)
            if not keyword:
                continue
            groups.setdefault(key, []).append(override)

        accumulated_groups = 0
        for (keyword, eval_id), group in groups.items():
            count = len(group)
            if count < EXCEPTION_ACCUMULATION_THRESHOLD:
                continue
            accumulated_groups += 1
            severity = "high" if count >= 10 else "medium"
            detail = (
                f"{count} overrides on similar context: '{keyword}' for "
                f"'{eval_id}'. Consider promoting to policy or eval rule."
            )
            flagged.append(
                {
                    "item_type": "exception_accumulation",
                    "item_id": f"{keyword}::{eval_id}",
                    "detail": detail,
                    "severity": severity,
                    "recommended_action": (
                        "Review override pattern. If accepted as norm, "
                        "create eval_case_candidate or update policy."
                    ),
                }
            )
            drift_signals.append(
                {
                    "signal_id": str(uuid.uuid4()),
                    "signal_type": "exception_accumulation",
                    "signal_strength": "strong" if severity == "high" else "moderate",
                    "affected_artifact_types": ["override_artifact"],
                    "detail": detail,
                    "baseline_value": None,
                    "current_value": float(count),
                    "delta_pct": None,
                    "detected_at": utcnow_iso(),
                }
            )

        prior_audit = find_prior_audit(repo_root_path, "exception_accumulation")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: Dict[str, Any] = {
            "total_overrides": len(all_overrides),
            "total_groups": len(groups),
            "accumulated_groups": accumulated_groups,
        }
        delta = None
        if prior_value is not None:
            delta = {
                "accumulated_groups": int(accumulated_groups)
                - int(prior_value.get("accumulated_groups", 0)),
            }

        status = "drift_detected" if flagged else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "exception_accumulation",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": len(all_overrides),
            "total_flagged": len(flagged),
            "status": status,
        }
        record["_drift_signals"] = drift_signals
        ok, err = validate_governance_artifact(
            {k: v for k, v in record.items() if not k.startswith("_")},
            "governance_audit_record",
        )
        if not ok:
            _LOG.warning("exception_accumulation audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
            record["_drift_signals"] = []
        persisted = {k: v for k, v in record.items() if not k.startswith("_")}
        write_audit_record(persisted, repo_root_path)
        return record
