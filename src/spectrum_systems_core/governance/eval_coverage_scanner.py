"""EvalCoverageScanner — uncovered artifact types, never-failing/degrading evals.

Reuses EvalScoreHistory from harness/eval_history.py.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ..harness.eval_history import EvalScoreHistory
from ._io import find_prior_audit, utcnow_iso, write_audit_record
from ._schema import validate_governance_artifact


_LOG = logging.getLogger(__name__)


def _list_eval_definitions(repo_root: Path) -> List[Dict[str, Any]]:
    eval_dir = repo_root / "contracts" / "evals"
    if not eval_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(eval_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
    return out


def _list_python_files(repo_root: Path) -> List[Path]:
    src = repo_root / "src"
    if not src.is_dir():
        return []
    return [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]


class EvalCoverageScanner:
    """Scan eval coverage for uncovered types and degrading evals."""

    def scan(self, repo_root: str | Path) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: List[Dict[str, Any]] = []

        evals = _list_eval_definitions(repo_root_path)
        eval_target_types = {
            d.get("target_artifact_type") for d in evals if d.get("target_artifact_type")
        }

        py_blob = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in _list_python_files(repo_root_path)
        )

        # Uncovered artifact types: schemas referenced in code but no eval targets them.
        schemas_root = repo_root_path / "contracts" / "schemas"
        if schemas_root.is_dir():
            for schema_path in sorted(schemas_root.rglob("*.schema.json")):
                try:
                    schema_doc = json.loads(
                        schema_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                title = schema_doc.get("title") if isinstance(schema_doc, dict) else None
                if not title or not isinstance(title, str):
                    continue
                # Only count schemas that are referenced by code.
                if (
                    schema_path.name not in py_blob
                    and schema_path.stem.replace(".schema", "") not in py_blob
                ):
                    continue
                if title not in eval_target_types:
                    flagged.append(
                        {
                            "item_type": "uncovered_artifact_type",
                            "item_id": title,
                            "detail": (
                                f"Artifact type '{title}' has no eval_case in "
                                "contracts/evals/"
                            ),
                            "severity": "high",
                            "recommended_action": (
                                f"Add eval coverage for artifact type '{title}'"
                            ),
                        }
                    )

        # Eval pass-rate signals via EvalScoreHistory.
        history = EvalScoreHistory()
        never_failing = 0
        never_passing = 0
        degrading = 0
        for definition in evals:
            artifact_type = definition.get("target_artifact_type")
            metric_name = definition.get("metric_name") or definition.get("name")
            if not artifact_type or not metric_name:
                continue
            stats = history.get_pass_rate(
                metric_name, artifact_type, repo_root_path, last_n_runs=30
            )
            total = int(stats.get("total") or 0)
            pass_rate = stats.get("pass_rate")
            if pass_rate is None:
                continue
            if pass_rate == 1.0 and total >= 20:
                never_failing += 1
                flagged.append(
                    {
                        "item_type": "never_failing_eval",
                        "item_id": f"{artifact_type}:{metric_name}",
                        "detail": (
                            f"Eval '{metric_name}' on {artifact_type} has "
                            f"pass_rate=1.0 over {total} runs"
                        ),
                        "severity": "low",
                        "recommended_action": (
                            "Consider strengthening or removing this eval"
                        ),
                    }
                )
            elif pass_rate == 0.0 and total >= 5:
                never_passing += 1
                flagged.append(
                    {
                        "item_type": "never_passing_eval",
                        "item_id": f"{artifact_type}:{metric_name}",
                        "detail": (
                            f"Eval '{metric_name}' on {artifact_type} has "
                            f"pass_rate=0.0 over {total} runs"
                        ),
                        "severity": "high",
                        "recommended_action": (
                            "Investigate consistent failure of this eval"
                        ),
                    }
                )
            elif pass_rate < 0.8 and total >= 10:
                degrading += 1
                flagged.append(
                    {
                        "item_type": "degrading_eval",
                        "item_id": f"{artifact_type}:{metric_name}",
                        "detail": (
                            f"Eval '{metric_name}' on {artifact_type} has "
                            f"pass_rate={pass_rate:.2f} over {total} runs"
                        ),
                        "severity": "medium",
                        "recommended_action": (
                            "Investigate recent failures and patterns"
                        ),
                    }
                )

        prior_audit = find_prior_audit(repo_root_path, "eval_coverage")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value = {
            "total_evals": len(evals),
            "never_failing": never_failing,
            "never_passing": never_passing,
            "degrading": degrading,
        }
        delta = None
        if prior_value is not None:
            delta = {
                k: int(current_value.get(k, 0)) - int(prior_value.get(k, 0))
                for k in current_value
            }

        status = "drift_detected" if flagged else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "eval_coverage",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": len(evals),
            "total_flagged": len(flagged),
            "status": status,
        }
        ok, err = validate_governance_artifact(record, "governance_audit_record")
        if not ok:
            _LOG.warning("eval_coverage audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
        write_audit_record(record, repo_root_path)
        return record
