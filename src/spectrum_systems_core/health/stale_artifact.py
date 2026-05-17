"""Stale artifact detection (Class 5).

Used by synthesize/bundle assembly. For each artifact in the bundle
compare ``created_at`` to the pipeline run's ``started_at``. Any
artifact older than :data:`MAX_ARTIFACT_AGE_HOURS` produces a warn.
If a majority of bundle artifacts are stale, the finding is
escalated to halt.

The threshold is sourced from
:func:`load_max_artifact_age_hours` so it can be overridden in
``config/health.json`` without code change.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .finding import HealthFinding

_LOG = logging.getLogger(__name__)

# Default threshold. Operators can override via the on-disk config
# below; tests rely on this being read at call time rather than at
# import time so they can monkey-patch the directory.
DEFAULT_MAX_ARTIFACT_AGE_HOURS: float = 48.0

# Stale majority => escalate warn to halt.
MAJORITY_STALE_THRESHOLD: float = 0.5


def _config_path(data_lake_path: str | Path) -> Path:
    return (
        Path(data_lake_path)
        / "store"
        / "artifacts"
        / "config"
        / "health.json"
    )


def load_max_artifact_age_hours(data_lake_path: str | Path) -> float:
    """Read ``MAX_ARTIFACT_AGE_HOURS`` from on-disk config, with default."""
    path = _config_path(data_lake_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_MAX_ARTIFACT_AGE_HOURS
    if not isinstance(data, dict):
        return DEFAULT_MAX_ARTIFACT_AGE_HOURS
    raw = data.get("max_artifact_age_hours")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return DEFAULT_MAX_ARTIFACT_AGE_HOURS


def _parse_iso(s: str) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class FreshnessResult:
    """Per-bundle freshness audit."""

    total: int
    stale: int
    findings: list[HealthFinding]

    @property
    def stale_ratio(self) -> float:
        if self.total == 0:
            return 0.0
        return self.stale / self.total


def check_artifact_freshness(
    artifact: dict[str, Any],
    pipeline_started_at: datetime,
    *,
    max_age_hours: float,
    pipeline_run_id: str | None = None,
) -> HealthFinding | None:
    created = _parse_iso(artifact.get("created_at", ""))
    if created is None:
        return None
    age_seconds = (pipeline_started_at - created).total_seconds()
    if age_seconds <= 0:
        return None
    age_hours = age_seconds / 3600.0
    if age_hours <= max_age_hours:
        return None
    return HealthFinding(
        finding_code="stale_artifact_in_bundle",
        severity="warn",
        pipeline_run_id=pipeline_run_id,
        context={
            "artifact_id": artifact.get("artifact_id", ""),
            "artifact_type": artifact.get("artifact_type", ""),
            "age_hours": round(age_hours, 1),
            "max_age_hours": max_age_hours,
        },
        remediation="Re-run pipeline with force=true to refresh stale artifacts.",
    )


def audit_bundle_freshness(
    artifacts: Iterable[dict[str, Any]],
    pipeline_started_at: datetime,
    *,
    data_lake_path: str | Path,
    pipeline_run_id: str | None = None,
) -> FreshnessResult:
    """Run :func:`check_artifact_freshness` over a bundle.

    Returns a :class:`FreshnessResult`. If more than 50% of the
    artifacts are stale, the last warn finding in the result is
    rewritten as a halt with the same context so the operator sees
    "majority stale" as a single signal rather than N warns.
    """
    max_age_hours = load_max_artifact_age_hours(data_lake_path)
    arts = list(artifacts)
    findings: list[HealthFinding] = []
    stale = 0
    for art in arts:
        f = check_artifact_freshness(
            art,
            pipeline_started_at,
            max_age_hours=max_age_hours,
            pipeline_run_id=pipeline_run_id,
        )
        if f is not None:
            stale += 1
            findings.append(f)

    result = FreshnessResult(total=len(arts), stale=stale, findings=findings)
    if result.total > 0 and result.stale_ratio > MAJORITY_STALE_THRESHOLD:
        # Append an aggregate halt so per-artifact context survives
        # and the operator sees the escalation as a single signal.
        findings.append(
            HealthFinding(
                finding_code="stale_artifact_in_bundle",
                severity="halt",
                pipeline_run_id=pipeline_run_id,
                context={
                    "aggregate": True,
                    "total": result.total,
                    "stale": result.stale,
                    "ratio": round(result.stale_ratio, 3),
                    "escalation_reason": "majority_stale",
                },
                remediation=(
                    "Majority of bundle artifacts are stale "
                    f"({result.stale}/{result.total}). Re-run pipeline "
                    "with force=true; do not proceed with synthesis."
                ),
            )
        )
    return result


def majority_stale(result: FreshnessResult) -> bool:
    """True when the result triggers the halt-escalation rule."""
    return (
        result.total > 0
        and result.stale_ratio > MAJORITY_STALE_THRESHOLD
    )
