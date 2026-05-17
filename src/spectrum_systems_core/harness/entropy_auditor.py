"""EntropyAuditor — flag entropy/complexity for human action.

FINDING-G-007: NEVER auto-deletes or auto-merges. Every flagged_item carries
a recommended_action a human acts on via CLI.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from . import OVERRIDE_EXPIRY_WARNING_DAYS
from ._io import (
    append_jsonl,
    read_jsonl,
    utcnow_iso,
)
from ._paths import (
    entropy_reports_path,
    evals_dir,
    overrides_dir,
    patterns_path,
    runs_archive_dir,
)
from ._schema import validate_harness_artifact
from .eval_history import EvalScoreHistory
from .outcome_memory import OutcomeMemoryStore
from .override_store import OverrideStore

_LOG = logging.getLogger(__name__)
_EFFECTIVENESS_THRESHOLD = 0.5
_ARCHIVE_GROWTH_THRESHOLD = 500


def _scan_idle_evals(repo_root: Path) -> list[dict[str, Any]]:
    """EVAL_CASE_NO_RECENT_FAILURES: evals with all-pass in last 30 runs."""
    flagged: list[dict[str, Any]] = []
    contracts_dir = repo_root / "contracts" / "evals"
    if not contracts_dir.is_dir():
        return flagged
    history = EvalScoreHistory()
    history_directory = evals_dir(repo_root)
    for case_file in sorted(contracts_dir.glob("*.json")):
        # Eval case files use "<artifact_type>_evals.json" naming.
        stem = case_file.stem
        if not stem.endswith("_evals"):
            continue
        artifact_type = stem[: -len("_evals")]
        history_file = history_directory / f"{artifact_type}_history.jsonl"
        records = read_jsonl(history_file)
        if not records:
            continue
        eval_names = sorted(
            {r.get("eval_name", "") for r in records if r.get("eval_name")}
        )
        for name in eval_names:
            stats = history.get_pass_rate(
                name, artifact_type, repo_root, last_n_runs=30
            )
            if stats["total"] >= 30 and stats["fail"] == 0 and stats["warn"] == 0:
                flagged.append(
                    {
                        "item_type": "eval_case",
                        "item_id": f"{artifact_type}:{name}",
                        "reason": (
                            f"eval has had 0 failures in the last "
                            f"{stats['total']} runs"
                        ),
                        "recommended_action": (
                            "consider removing or strengthening this eval"
                        ),
                        "severity": "low",
                    }
                )
    return flagged


def _scan_patterns_without_candidate(repo_root: Path) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    patterns = read_jsonl(patterns_path(repo_root))
    for pattern in patterns:
        if int(pattern.get("occurrence_count", 0)) < 3:
            continue
        if pattern.get("eval_candidate_id"):
            continue
        flagged.append(
            {
                "item_type": "failure_pattern",
                "item_id": str(pattern.get("pattern_id") or ""),
                "reason": (
                    f"pattern '{pattern.get('reason_code', '')}' has "
                    f"{pattern.get('occurrence_count', 0)} occurrences but "
                    "no eval candidate"
                ),
                "recommended_action": (
                    "run: python -m spectrum_systems_core.cli "
                    "propose-eval-candidate --pattern-id "
                    f"{pattern.get('pattern_id', '')}"
                ),
                "severity": "medium",
            }
        )
    return flagged


def _scan_expiring_overrides(repo_root: Path) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    store = OverrideStore()
    actives = store.get_active_overrides(repo_root)
    for override in actives:
        if not override.get("_warning"):
            continue
        flagged.append(
            {
                "item_type": "override",
                "item_id": str(override.get("override_id") or ""),
                "reason": (
                    f"override expires at {override.get('expires_at', '')} "
                    f"(within {OVERRIDE_EXPIRY_WARNING_DAYS} days)"
                ),
                "recommended_action": (
                    f"Review and renew or retire override "
                    f"{override.get('override_id', '')}"
                ),
                "severity": "high",
            }
        )
    return flagged


def _scan_outcome_effectiveness(repo_root: Path) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    store = OutcomeMemoryStore()
    for outcome_type in ("revision", "mitigation"):
        stats = store.get_effectiveness_rate(outcome_type, repo_root)
        rate = stats.get("effectiveness_rate")
        if rate is None:
            continue
        if rate < _EFFECTIVENESS_THRESHOLD:
            flagged.append(
                {
                    "item_type": "outcome_effectiveness",
                    "item_id": outcome_type,
                    "reason": (
                        f"{outcome_type} effectiveness {rate * 100:.1f}% over "
                        f"{stats['total']} records"
                    ),
                    "recommended_action": (
                        f"Review recent {outcome_type} strategies — "
                        "effectiveness below 50%"
                    ),
                    "severity": "medium",
                }
            )
    return flagged


def _scan_archive_growth(repo_root: Path) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    archive = runs_archive_dir(repo_root)
    if not archive.is_dir():
        return flagged
    files = [f for f in archive.iterdir() if f.is_file() and f.suffix == ".json"]
    count = len(files)
    if count > _ARCHIVE_GROWTH_THRESHOLD:
        flagged.append(
            {
                "item_type": "run_archive",
                "item_id": "runs/archive",
                "reason": (
                    f"runs/archive has {count} files "
                    f"(> {_ARCHIVE_GROWTH_THRESHOLD})"
                ),
                "recommended_action": (
                    f"Consider purging archives older than 1 year — "
                    f"currently {count} files"
                ),
                "severity": "low",
            }
        )
    return flagged


def _count_scanned(repo_root: Path) -> int:
    total = 0
    contracts_dir = repo_root / "contracts" / "evals"
    if contracts_dir.is_dir():
        total += sum(1 for _ in contracts_dir.glob("*.json"))
    total += len(read_jsonl(patterns_path(repo_root)))
    overrides = overrides_dir(repo_root)
    if overrides.is_dir():
        total += sum(1 for f in overrides.glob("*.json"))
    return total


class EntropyAuditor:
    def run_audit(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> dict[str, Any]:
        try:
            repo_root_path = Path(repo_root).resolve()
            flagged: list[dict[str, Any]] = []
            flagged.extend(_scan_idle_evals(repo_root_path))
            flagged.extend(_scan_patterns_without_candidate(repo_root_path))
            flagged.extend(_scan_expiring_overrides(repo_root_path))
            flagged.extend(_scan_outcome_effectiveness(repo_root_path))
            flagged.extend(_scan_archive_growth(repo_root_path))

            report = {
                "report_id": str(uuid.uuid4()),
                "generated_at": utcnow_iso(),
                "scope": "harness_memory_full_audit",
                "flagged_items": flagged,
                "total_flagged": len(flagged),
                "total_scanned": _count_scanned(repo_root_path),
            }
            ok, err = validate_harness_artifact(report, "entropy_report")
            if not ok:
                return {
                    "status": "failure",
                    "report_id": "",
                    "reason": f"schema_violation: {err}",
                }
            append_jsonl(entropy_reports_path(repo_root_path), report)
            self.write_entropy_projection(report, repo_root_path, vault_root)
            return {
                "status": "success",
                "report_id": report["report_id"],
                "total_flagged": report["total_flagged"],
                "report": report,
            }
        except Exception as exc:  # pragma: no cover
            _LOG.warning("EntropyAuditor.run_audit failed: %s", exc)
            return {
                "status": "failure",
                "report_id": "",
                "reason": f"unexpected_error: {exc}",
            }

    def write_entropy_projection(
        self,
        report: dict[str, Any],
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_entropy_projection(
            report, repo_root, vault_root
        )
