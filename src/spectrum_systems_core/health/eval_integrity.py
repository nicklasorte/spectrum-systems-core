"""Eval integrity checks (Classes 1, 2, 4, 7).

Runs around eval scoring:

* Class 1+2: upstream-failure gating + zero-cause annotation.
* Class 4:   eval-pair coverage audit.
* Class 7:   model-registry drift detection.

Rollback: ``EVAL_INTEGRITY_ENABLED=false`` skips all of these. A
warning is logged on bypass.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .finding import HealthFinding, write_finding

_LOG = logging.getLogger(__name__)

EVAL_INTEGRITY_ENV_VAR: str = "EVAL_INTEGRITY_ENABLED"
_DISABLED_VALUES: frozenset[str] = frozenset({"false", "0", "no", "off"})


def eval_integrity_enabled() -> bool:
    raw = os.environ.get(EVAL_INTEGRITY_ENV_VAR, "")
    if raw.strip().lower() in _DISABLED_VALUES:
        _LOG.warning(
            "eval_integrity_disabled: %s=false -- skipping eval integrity "
            "checks. This is a deliberate bypass.",
            EVAL_INTEGRITY_ENV_VAR,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Class 1 + 2: upstream health
# ---------------------------------------------------------------------------


@dataclass
class UpstreamHealth:
    synthesize_succeeded: bool
    chunks_blocked: int
    block_rate: float
    record_present: bool = True
    raw_record: Optional[dict[str, Any]] = None


def _orchestration_dir_candidates(data_lake_path: str | Path) -> list[Path]:
    return [
        Path(data_lake_path) / "store" / "artifacts" / "orchestration",
        Path(data_lake_path) / "store" / "orchestration",
    ]


def _load_orchestration(
    pipeline_run_id: str, data_lake_path: str | Path
) -> Optional[dict[str, Any]]:
    for d in _orchestration_dir_candidates(data_lake_path):
        if not d.is_dir():
            continue
        # Most reliable lookup: scan and match run_id.
        for path in d.glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("run_id") == pipeline_run_id:
                return rec
    return None


def check_upstream_health(
    pipeline_run_id: str, data_lake_path: str | Path
) -> UpstreamHealth:
    """Read the orchestration_result artifact for ``pipeline_run_id``.

    Returns a record with ``record_present=False`` when no artifact
    exists (e.g. first-ever pipeline run). Callers should treat that
    as an info-level "no prior run" signal, not as a failure.
    """
    rec = _load_orchestration(pipeline_run_id, data_lake_path)
    if rec is None:
        return UpstreamHealth(
            synthesize_succeeded=True,
            chunks_blocked=0,
            block_rate=0.0,
            record_present=False,
        )
    stage_status = rec.get("stage_status", "ok")
    chunks_attempted = int(rec.get("chunks_attempted", 0) or 0)
    chunks_blocked = int(rec.get("chunks_blocked", 0) or 0)
    chunks_succeeded = int(rec.get("chunks_succeeded", 0) or 0)
    block_rate = 0.0
    if chunks_attempted > 0:
        block_rate = chunks_blocked / chunks_attempted
    # "synthesize failed" maps to stage_status == "failed" OR
    # no chunks succeeded when we attempted any.
    synthesize_succeeded = stage_status != "failed" and not (
        chunks_attempted > 0 and chunks_succeeded == 0
    )
    return UpstreamHealth(
        synthesize_succeeded=synthesize_succeeded,
        chunks_blocked=chunks_blocked,
        block_rate=block_rate,
        record_present=True,
        raw_record=rec,
    )


def evaluate_upstream(
    pipeline_run_id: str,
    data_lake_path: str | Path,
    *,
    scores_are_zero: Optional[bool] = None,
) -> tuple[List[HealthFinding], bool]:
    """Compute upstream findings and whether eval should proceed.

    Returns ``(findings, should_run_eval)``.

    * synthesize failed -> single ``upstream_failure_eval_blocked`` halt;
      eval must NOT run. If ``scores_are_zero`` is True, also emits the
      ``eval_zero_cause_upstream`` warn so the operator gets cause
      attribution.
    * synthesize succeeded with ``chunks_blocked > 0`` -> warn,
      eval proceeds (callers must annotate eval_summary).
    * clean upstream + zero scores -> info finding
      ``eval_zero_cause_extraction``.
    * no orchestration record -> info ``no_prior_orchestration_artifact``;
      eval proceeds.
    """
    findings: list[HealthFinding] = []
    health = check_upstream_health(pipeline_run_id, data_lake_path)

    if not health.record_present:
        findings.append(
            HealthFinding(
                finding_code="no_prior_orchestration_artifact",
                severity="info",
                pipeline_run_id=pipeline_run_id,
                context={"pipeline_run_id": pipeline_run_id},
                remediation=(
                    "No prior orchestration_result for this run_id. "
                    "Eval is proceeding. This is normal on the first "
                    "pipeline run."
                ),
            )
        )
        return findings, True

    if not health.synthesize_succeeded:
        findings.append(
            HealthFinding(
                finding_code="upstream_failure_eval_blocked",
                severity="halt",
                pipeline_run_id=pipeline_run_id,
                context={
                    "pipeline_run_id": pipeline_run_id,
                    "chunks_blocked": health.chunks_blocked,
                    "block_rate": round(health.block_rate, 3),
                    "stage_status": (
                        health.raw_record or {}
                    ).get("stage_status"),
                },
                remediation=(
                    "Synthesize failed upstream. Fix synthesize before "
                    "scoring; eval output would be indistinguishable "
                    "from a genuine zero score."
                ),
            )
        )
        if scores_are_zero:
            findings.append(
                HealthFinding(
                    finding_code="eval_zero_cause_upstream",
                    severity="warn",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "pipeline_run_id": pipeline_run_id,
                        "explanation": (
                            "Eval zeros are caused by upstream synthesize "
                            "failure, not extraction quality."
                        ),
                    },
                    remediation="Diagnose synthesize failure; do not retune extractor.",
                )
            )
        return findings, False

    if health.chunks_blocked > 0:
        findings.append(
            HealthFinding(
                finding_code="upstream_failure_eval_invalid",
                severity="warn",
                pipeline_run_id=pipeline_run_id,
                context={
                    "pipeline_run_id": pipeline_run_id,
                    "chunks_blocked": health.chunks_blocked,
                    "block_rate": round(health.block_rate, 3),
                    "scores_may_be_understated": True,
                },
                remediation=(
                    f"{health.chunks_blocked} chunks blocked upstream. "
                    "Scores may understate actual quality."
                ),
            )
        )
        return findings, True

    if scores_are_zero:
        findings.append(
            HealthFinding(
                finding_code="eval_zero_cause_extraction",
                severity="info",
                pipeline_run_id=pipeline_run_id,
                context={
                    "pipeline_run_id": pipeline_run_id,
                    "explanation": (
                        "Upstream is clean; zero scores reflect "
                        "extraction quality, not upstream failure."
                    ),
                },
                remediation=(
                    "Review extractor output for the affected pairs."
                ),
            )
        )

    return findings, True


def upstream_health_annotation(health: UpstreamHealth) -> dict[str, Any]:
    """Annotation block to attach to the eval_summary artifact."""
    return {
        "chunks_blocked": health.chunks_blocked,
        "block_rate": round(health.block_rate, 3),
        "synthesize_succeeded": health.synthesize_succeeded,
        "scores_may_be_understated": health.chunks_blocked > 0,
    }


# ---------------------------------------------------------------------------
# Class 4: pair coverage audit
# ---------------------------------------------------------------------------


@dataclass
class PairAudit:
    total_pairs: int
    confirmed: int
    pending_review: int
    evaluated: int
    missing_from_eval: int
    pending_pair_ids: list[str] = field(default_factory=list)
    missing_pair_ids: list[str] = field(default_factory=list)


def audit_pair_coverage(
    eval_results: Sequence[dict[str, Any]],
    ground_truth_pairs: Sequence[dict[str, Any]],
) -> PairAudit:
    """Count pairs in each state. Surface every exclusion."""
    confirmed = [p for p in ground_truth_pairs if p.get("status") == "confirmed"]
    pending = [p for p in ground_truth_pairs if p.get("status") == "pending_review"]
    evaluated_ids = {r.get("pair_id") for r in eval_results if r.get("pair_id")}
    missing = [
        p.get("pair_id")
        for p in confirmed
        if p.get("pair_id") and p.get("pair_id") not in evaluated_ids
    ]
    return PairAudit(
        total_pairs=len(ground_truth_pairs),
        confirmed=len(confirmed),
        pending_review=len(pending),
        evaluated=len(evaluated_ids),
        missing_from_eval=len(missing),
        pending_pair_ids=[p.get("pair_id", "") for p in pending if p.get("pair_id")],
        missing_pair_ids=[m for m in missing if m],
    )


def pair_audit_finding(
    audit: PairAudit, *, pipeline_run_id: Optional[str] = None
) -> Optional[HealthFinding]:
    if audit.pending_review == 0 and audit.missing_from_eval == 0:
        return None
    return HealthFinding(
        finding_code="eval_pairs_excluded",
        severity="warn",
        pipeline_run_id=pipeline_run_id,
        context={
            "total_pairs": audit.total_pairs,
            "confirmed": audit.confirmed,
            "pending_review": audit.pending_review,
            "evaluated": audit.evaluated,
            "missing_from_eval": audit.missing_from_eval,
            "pending_pair_ids": list(audit.pending_pair_ids),
            "missing_pair_ids": list(audit.missing_pair_ids),
        },
        remediation=(
            f"{audit.pending_review + audit.missing_from_eval} pairs "
            f"excluded: {audit.pending_review} pending_review, "
            f"{audit.missing_from_eval} missing from eval results. "
            "Confirm pending pairs in the review queue and re-run eval."
        ),
    )


# ---------------------------------------------------------------------------
# Class 7: model registry drift
# ---------------------------------------------------------------------------


def _registry_path(data_lake_path: str | Path) -> Path:
    return (
        Path(data_lake_path)
        / "store"
        / "artifacts"
        / "config"
        / "model_registry.json"
    )


def get_registry_hash(data_lake_path: str | Path) -> Optional[str]:
    """Return a 16-char prefix of the sha256 of the model registry.

    Returns ``None`` if the registry file is not present so callers
    can omit the annotation rather than crash.
    """
    path = _registry_path(data_lake_path)
    try:
        content = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(content).hexdigest()[:16]


def _registry_models(data_lake_path: str | Path) -> dict[str, Any]:
    path = _registry_path(data_lake_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _diff_model_keys(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    keys = set(current.keys()) | set(baseline.keys())
    changed: list[str] = []
    for k in sorted(keys):
        if current.get(k) != baseline.get(k):
            changed.append(k)
    return changed


def detect_registry_drift(
    data_lake_path: str | Path,
    baseline_hash: Optional[str],
    *,
    baseline_models: Optional[dict[str, Any]] = None,
    pipeline_run_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[HealthFinding]]:
    """Compute current registry hash and a drift finding if it differs.

    Returns ``(current_hash, finding_or_None)``. When no baseline is
    supplied the function returns just the hash and no finding -- the
    caller still records the hash on the eval_summary.

    Drift never blocks the eval (severity: warn).
    """
    current_hash = get_registry_hash(data_lake_path)
    if current_hash is None or baseline_hash is None:
        return current_hash, None
    if current_hash == baseline_hash:
        return current_hash, None
    current_models = _registry_models(data_lake_path)
    changed = _diff_model_keys(current_models, baseline_models or {})
    finding = HealthFinding(
        finding_code="model_registry_drift",
        severity="warn",
        pipeline_run_id=pipeline_run_id,
        context={
            "baseline_hash": baseline_hash,
            "current_hash": current_hash,
            "changed_models": changed,
        },
        remediation=(
            "Model registry changed since baseline. Score changes may "
            "reflect model-version change rather than extraction-quality "
            "change. Confirm before treating delta as regression."
        ),
    )
    return current_hash, finding


# ---------------------------------------------------------------------------
# Persist + summary helpers
# ---------------------------------------------------------------------------


def persist_findings(
    findings: Iterable[HealthFinding],
    *,
    data_lake_path: str | Path,
) -> list[Path]:
    """Write each finding via :func:`write_finding`. Returns the paths."""
    paths: list[Path] = []
    for f in findings:
        try:
            paths.append(write_finding(f, data_lake_path=data_lake_path))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "health_finding_write_failed: code=%s err=%s",
                f.finding_code, exc,
            )
    return paths


def append_github_summary(
    findings: Iterable[HealthFinding],
    *,
    blocked_message: Optional[str] = None,
) -> None:
    """Append the health-check Markdown table to GITHUB_STEP_SUMMARY.

    When ``blocked_message`` is given (Class 1 halt path), the summary
    leads with that message rather than the canonical ``0.000``. This
    is the spec's "Eval blocked: ..." surface.
    """
    gh_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not gh_path:
        return
    findings = list(findings)
    try:
        with open(gh_path, "a", encoding="utf-8") as fh:
            if blocked_message:
                fh.write(f"\n{blocked_message}\n\n")
            fh.write("## Health Check Results\n\n")
            if not findings:
                fh.write("OK All health checks passed.\n")
                return
            fh.write("| Finding | Severity | Remediation |\n")
            fh.write("|---------|----------|-------------|\n")
            for f in findings:
                icon = {"halt": "HALT", "warn": "WARN", "info": "INFO"}[
                    f.severity
                ]
                fh.write(
                    f"| {f.finding_code} | {icon} | "
                    f"{f.remediation.replace('|', '/')} |\n"
                )
    except OSError as exc:
        _LOG.warning("github_step_summary_write_failed: %s", exc)
