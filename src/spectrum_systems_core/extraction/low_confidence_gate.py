"""Phase T.6: low-confidence routing gate (correction-mining seed).

After typed extraction completes for a transcript, scan the produced
decisions and claims for items below ``LOW_CONF_CONFIDENCE_THRESHOLD``.
If the share of low-confidence items exceeds ``LOW_CONF_RATE_LIMIT``,
emit a ``low_confidence_extraction`` warn finding AND write a
``correction_candidate`` artifact to
``<sdl_root>/correction_candidates/<source_id>/<uuid>.json``.

This is the *seed* of the correction-mining loop. The artifact carries
the low-confidence items, a stable correction_candidate_id, and an
``expires_at`` 30 days after creation. The preflight scanner later
emits ``correction_candidate_expired`` info findings; no auto-deletion
-- the human reviewer owns disposition.

Feature flag ``LOW_CONFIDENCE_GATE_ENABLED=false`` skips the entire
gate. Rollback path = env var, not code revert.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..health.finding import HealthFinding

_LOG = logging.getLogger(__name__)


LOW_CONFIDENCE_GATE_ENABLED_ENV: str = "LOW_CONFIDENCE_GATE_ENABLED"
LOW_CONF_CONFIDENCE_THRESHOLD_ENV: str = "LOW_CONF_CONFIDENCE_THRESHOLD"
LOW_CONF_RATE_LIMIT_ENV: str = "LOW_CONF_RATE_LIMIT"
CORRECTION_CANDIDATE_TTL_DAYS_ENV: str = "CORRECTION_CANDIDATE_TTL_DAYS"

_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})

_DEFAULT_CONFIDENCE_THRESHOLD: float = 0.6
_DEFAULT_RATE_LIMIT: float = 0.30
_DEFAULT_TTL_DAYS: int = 30


def gate_enabled() -> bool:
    """Default ON. Set env var to a disabled value to skip the gate."""
    raw = os.environ.get(LOW_CONFIDENCE_GATE_ENABLED_ENV, "").strip().lower()
    if raw in _DISABLED_VALUES:
        return False
    return True


def _resolve_float(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v < 0.0 or v > 1.0:
        return default
    return v


def _resolve_ttl_days() -> int:
    raw = os.environ.get(CORRECTION_CANDIDATE_TTL_DAYS_ENV, "").strip()
    if not raw:
        return _DEFAULT_TTL_DAYS
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_TTL_DAYS
    if v <= 0:
        return _DEFAULT_TTL_DAYS
    return v


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _is_low_conf(item: Dict[str, Any], threshold: float) -> bool:
    conf = item.get("confidence")
    if not isinstance(conf, (int, float)):
        return False
    return float(conf) < threshold


def build_correction_candidate(
    *,
    source_id: str,
    low_conf_decisions: List[Dict[str, Any]],
    low_conf_claims: List[Dict[str, Any]],
    rate: float,
    threshold: float,
    rate_limit: float,
    extraction_run_id: Optional[str] = None,
    ttl_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Construct a correction_candidate artifact dict."""
    ttl = ttl_days if isinstance(ttl_days, int) and ttl_days > 0 else _resolve_ttl_days()
    created = _now()
    expires = created + datetime.timedelta(days=ttl)
    return {
        "artifact_type": "correction_candidate",
        "schema_version": "1.0.0",
        "correction_candidate_id": str(uuid.uuid4()),
        "source_id": source_id or "",
        "created_at": _iso(created),
        "expires_at": _iso(expires),
        "low_confidence_decisions": list(low_conf_decisions),
        "low_confidence_claims": list(low_conf_claims),
        "low_confidence_rate": round(float(rate), 6),
        "low_confidence_threshold": float(threshold),
        "rate_limit": float(rate_limit),
        "status": "pending",
        "extraction_run_id": extraction_run_id or "",
    }


def write_correction_candidate(
    artifact: Dict[str, Any],
    *,
    sdl_root: Path,
) -> Optional[Path]:
    """Persist the correction_candidate under
    ``<sdl_root>/correction_candidates/<source_id>/<uuid>.json``.

    Returns the write path or ``None`` on failure. Validation failure
    is logged; the artifact is still written so the operator has the
    forensic record even when the schema drifts.
    """
    source_id = str(artifact.get("source_id") or "unknown")
    cid = str(artifact.get("correction_candidate_id") or uuid.uuid4().hex)
    target_dir = Path(sdl_root) / "correction_candidates" / source_id
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("correction_candidate_mkdir_failed: %s", exc)
        return None
    target = target_dir / f"{cid}.json"

    try:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )
        try:
            validate_artifact(artifact, "correction_candidate")
        except (ArtifactValidationError, SchemaNotFoundError) as exc:
            _LOG.warning(
                "correction_candidate_schema_violation: %s", exc,
            )
    except ImportError:
        pass

    try:
        target.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target
    except OSError as exc:
        _LOG.warning("correction_candidate_write_failed: %s", exc)
        return None


def check_low_confidence(
    extraction_artifact: Dict[str, Any],
    *,
    source_id: str,
    sdl_root: Optional[Path] = None,
    extraction_run_id: Optional[str] = None,
    pipeline_run_id: Optional[str] = None,
    threshold: Optional[float] = None,
    rate_limit: Optional[float] = None,
) -> Tuple[List[HealthFinding], Optional[Path]]:
    """Apply the gate to a meeting_extraction artifact.

    Returns ``(findings, correction_candidate_path_or_none)``. Findings
    are empty unless the low-confidence rate exceeds the limit. When
    fired, a single ``low_confidence_extraction`` warn finding is
    returned and -- if sdl_root is provided -- a correction_candidate
    artifact is written.

    Defaults:
      - threshold = LOW_CONF_CONFIDENCE_THRESHOLD env var, else 0.6
      - rate_limit = LOW_CONF_RATE_LIMIT env var, else 0.30
    """
    if not gate_enabled():
        return [], None

    if not isinstance(extraction_artifact, dict):
        return [], None

    if threshold is None:
        threshold = _resolve_float(
            LOW_CONF_CONFIDENCE_THRESHOLD_ENV, _DEFAULT_CONFIDENCE_THRESHOLD,
        )
    if rate_limit is None:
        rate_limit = _resolve_float(
            LOW_CONF_RATE_LIMIT_ENV, _DEFAULT_RATE_LIMIT,
        )

    decisions = extraction_artifact.get("decisions") or []
    claims = extraction_artifact.get("claims") or []
    if not isinstance(decisions, list):
        decisions = []
    if not isinstance(claims, list):
        claims = []

    low_dec = [d for d in decisions if isinstance(d, dict) and _is_low_conf(d, threshold)]
    low_clm = [c for c in claims if isinstance(c, dict) and _is_low_conf(c, threshold)]

    total = len(decisions) + len(claims)
    low_total = len(low_dec) + len(low_clm)
    rate = (low_total / float(total)) if total > 0 else 0.0

    if rate <= rate_limit:
        return [], None

    findings = [
        HealthFinding(
            finding_code="low_confidence_extraction",
            severity="warn",
            pipeline_run_id=pipeline_run_id,
            context={
                "source_id": source_id or "",
                "low_confidence_rate": round(float(rate), 6),
                "low_conf_decisions": len(low_dec),
                "low_conf_claims": len(low_clm),
                "threshold": float(threshold),
                "rate_limit": float(rate_limit),
            },
            remediation=(
                "More than the configured share of decisions/claims "
                "scored below the confidence threshold. A "
                "correction_candidate artifact has been written under "
                "<sdl_root>/correction_candidates/<source_id>/ so a "
                "human reviewer can triage. Adjust the threshold via "
                "LOW_CONF_CONFIDENCE_THRESHOLD if the model's "
                "calibration has shifted."
            ),
        )
    ]

    candidate_path: Optional[Path] = None
    if sdl_root is not None:
        artifact = build_correction_candidate(
            source_id=source_id,
            low_conf_decisions=low_dec,
            low_conf_claims=low_clm,
            rate=rate,
            threshold=float(threshold),
            rate_limit=float(rate_limit),
            extraction_run_id=extraction_run_id,
        )
        candidate_path = write_correction_candidate(artifact, sdl_root=sdl_root)

    return findings, candidate_path


def count_pending_candidates(
    sdl_root: Path,
    *,
    source_id: Optional[str] = None,
) -> int:
    """Count correction_candidate artifacts with status=pending that have not expired."""
    root = Path(sdl_root) / "correction_candidates"
    if source_id:
        root = root / source_id
    if not root.is_dir():
        return 0
    now = _now()
    count = 0
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("artifact_type") != "correction_candidate":
            continue
        if data.get("status") != "pending":
            continue
        exp = data.get("expires_at")
        if isinstance(exp, str) and exp:
            try:
                exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt < now:
                    continue
            except ValueError:
                pass
        count += 1
    return count


def scan_expired_candidates(
    sdl_root: Path,
    *,
    pipeline_run_id: Optional[str] = None,
) -> List[HealthFinding]:
    """Walk correction_candidates/ and return one info finding per expired pending artifact."""
    findings: List[HealthFinding] = []
    root = Path(sdl_root) / "correction_candidates"
    if not root.is_dir():
        return findings
    now = _now()
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("artifact_type") != "correction_candidate":
            continue
        if data.get("status") != "pending":
            continue
        exp = data.get("expires_at")
        if not isinstance(exp, str) or not exp:
            continue
        try:
            exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if exp_dt >= now:
            continue
        findings.append(
            HealthFinding(
                finding_code="correction_candidate_expired",
                severity="info",
                pipeline_run_id=pipeline_run_id,
                context={
                    "correction_candidate_id": str(
                        data.get("correction_candidate_id") or ""
                    ),
                    "source_id": str(data.get("source_id") or ""),
                    "expires_at": exp,
                    "path": str(path),
                },
                remediation=(
                    "Correction candidate exceeded its TTL. Either "
                    "promote the human-curated correction back into "
                    "the eval baseline, or set status=discarded so "
                    "the preflight stops flagging it."
                ),
            )
        )
    return findings


__all__ = [
    "CORRECTION_CANDIDATE_TTL_DAYS_ENV",
    "LOW_CONFIDENCE_GATE_ENABLED_ENV",
    "LOW_CONF_CONFIDENCE_THRESHOLD_ENV",
    "LOW_CONF_RATE_LIMIT_ENV",
    "build_correction_candidate",
    "check_low_confidence",
    "count_pending_candidates",
    "gate_enabled",
    "scan_expired_candidates",
    "write_correction_candidate",
]
